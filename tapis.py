#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["garminconnect>=0.2.30"]
# ///
"""Corrige une activité « tapis de marche » enregistrée par la montre Garmin.

Quand on marche sur un tapis en travaillant, les mains restent sur le clavier :
la montre ne compte presque aucun pas, la distance est fausse (quelques centaines
de mètres pour 1 h 30), le temps de déplacement est quasi nul et les calories
ne suivent pas l'effort.

L'outil réécrit le fichier FIT d'origine (toutes les autres données, fréquence
cardiaque comprise, restent intactes) avec :
  - la distance lue sur le tapis, répartie régulièrement sur le temps de marche ;
  - la vitesse correspondante (le temps de déplacement redevient juste) ;
  - les pas et la cadence (estimés d'après ta taille, ou ceux du tapis) ;
  - les calories (celles du tapis, ou une estimation ACSM selon ton poids et la pente).

Usage :
  ./tapis.py                         dernière marche Garmin : corrige, supprime, renvoie
  ./tapis.py --distance 4.5          idem sans question sur la distance
  ./tapis.py --id 24987031322        une activité précise
  ./tapis.py --fichier act.fit --distance 4.5   hors ligne : écrit seulement le FIT corrigé
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DOSSIER = Path.home() / "Garmin-tapis"
TOKENS = DOSSIER / "jetons"  # propres à l'outil, pour ne pas toucher ~/.garminconnect
SAUVEGARDES = DOSSIER / "sauvegardes"
FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC

# Messages FIT globaux utiles
MSG_RECORD, MSG_LAP, MSG_SESSION, MSG_EVENT, MSG_USER_PROFILE = 20, 19, 18, 21, 3

# Numéros de champs (profil FIT)
REC = {"cadence": 4, "distance": 5, "speed": 6, "enhanced_speed": 73, "fractional_cadence": 53}
SES = {
    "total_distance": 9, "total_strides": 10, "total_calories": 11,
    "avg_speed": 14, "max_speed": 15, "avg_cadence": 18, "max_cadence": 19,
    "avg_fractional_cadence": 92, "max_fractional_cadence": 93,
    "enhanced_avg_speed": 124, "enhanced_max_speed": 125,
}
LAP = {
    "total_distance": 9, "total_strides": 10, "total_calories": 11,
    "avg_speed": 13, "max_speed": 14, "avg_cadence": 17, "max_cadence": 18,
    "avg_fractional_cadence": 80, "max_fractional_cadence": 81,
    "enhanced_avg_speed": 110, "enhanced_max_speed": 111,
}

# Format struct par type de base FIT (octet de type & 0x1F)
BASE_FMT = {0: "B", 1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 10: "B",
            11: "h", 12: "H", 13: "B", 14: "q", 15: "Q", 16: "Q", 8: "f", 9: "d"}


# --------------------------------------------------------------------------- FIT

def crc16(data: bytes, crc: int = 0) -> int:
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


@dataclass
class Champ:
    num: int
    offset: int  # position absolue dans le fichier
    size: int
    fmt: str     # format struct complet ('<I', '>H'…)


@dataclass
class Message:
    glob: int
    timestamp: int | None
    champs: dict[int, Champ] = field(default_factory=dict)


class FitFile:
    """Lecture minimale d'un FIT, assez pour modifier des valeurs sur place."""

    def __init__(self, data: bytes):
        self.data = bytearray(data)
        header_size = self.data[0]
        if self.data[8:12] != b".FIT":
            raise ValueError("Ce n'est pas un fichier FIT.")
        data_size = struct.unpack_from("<I", self.data, 4)[0]
        self.fin = header_size + data_size
        self.messages: list[Message] = []
        self._lire(header_size)

    def _lire(self, pos: int) -> None:
        defs: dict[int, tuple] = {}
        dernier_ts = 0
        d = self.data
        while pos < self.fin:
            h = d[pos]
            pos += 1
            if h & 0x80:  # en-tête à horodatage compressé
                local = (h >> 5) & 0x03
                off = h & 0x1F
                ts = (dernier_ts & ~0x1F) + off
                if off < (dernier_ts & 0x1F):
                    ts += 0x20
                dernier_ts = ts
                ts_compresse = ts
            elif h & 0x40:  # définition
                local = h & 0x0F
                arch = d[pos + 1]
                endian = ">" if arch == 1 else "<"
                glob = struct.unpack_from(endian + "H", d, pos + 2)[0]
                n = d[pos + 4]
                pos += 5
                champs = []
                for _ in range(n):
                    champs.append((d[pos], d[pos + 1], d[pos + 2]))
                    pos += 3
                dev = 0
                if h & 0x20:
                    nd = d[pos]
                    pos += 1
                    for _ in range(nd):
                        dev += d[pos + 1]
                        pos += 3
                defs[local] = (glob, endian, champs, dev)
                continue
            else:
                local = h & 0x0F
                ts_compresse = None

            glob, endian, champs, dev = defs[local]
            msg = Message(glob, ts_compresse)
            for num, size, base in champs:
                fmt = BASE_FMT.get(base & 0x1F)
                if fmt and struct.calcsize(fmt) == size:
                    msg.champs[num] = Champ(num, pos, size, endian + fmt)
                    if num == 253:
                        ts = struct.unpack_from(endian + fmt, d, pos)[0]
                        msg.timestamp = ts
                        dernier_ts = ts
                pos += size
            pos += dev
            self.messages.append(msg)

    def lire(self, msg: Message, num: int):
        c = msg.champs.get(num)
        return None if c is None else struct.unpack_from(c.fmt, self.data, c.offset)[0]

    def ecrire(self, msg: Message, num: int, valeur: float) -> None:
        c = msg.champs.get(num)
        if c is None:
            return
        maxi = (1 << (8 * c.size)) - 2  # la valeur max est réservée à « invalide »
        struct.pack_into(c.fmt, self.data, c.offset, max(0, min(int(round(valeur)), maxi)))

    def octets(self) -> bytes:
        corps = bytes(self.data[: self.fin])
        return corps + struct.pack("<H", crc16(corps))

    def du_type(self, glob: int) -> list[Message]:
        return [m for m in self.messages if m.glob == glob]


# ------------------------------------------------------------------- correction

@dataclass
class Resultat:
    debut: datetime
    duree_min: float
    distance_avant_m: float
    distance_m: float
    calories_avant: int
    calories: int
    pas: int
    vitesse_kmh: float


def secondes_actives(fit: FitFile) -> list[tuple[int, int]]:
    """Intervalles où le chrono tournait (événements timer start/stop)."""
    intervalles, debut = [], None
    for m in fit.du_type(MSG_EVENT):
        if fit.lire(m, 0) != 0:  # event = timer
            continue
        typ = fit.lire(m, 1)  # 0 start, 1 stop, 4 stop_all
        if typ == 0 and debut is None:
            debut = m.timestamp
        elif typ in (1, 4) and debut is not None:
            intervalles.append((debut, m.timestamp))
            debut = None
    recs = fit.du_type(MSG_RECORD)
    if debut is not None:
        intervalles.append((debut, recs[-1].timestamp))
    if not intervalles:
        intervalles = [(recs[0].timestamp, recs[-1].timestamp)]
    return intervalles


def calories_acsm(poids_kg: float, vitesse_kmh: float, pente_pct: float, minutes: float) -> int:
    """Équation ACSM de la marche (dépense brute, repos compris)."""
    v = vitesse_kmh * 1000 / 60  # m/min
    vo2 = 3.5 + 0.1 * v + 1.8 * v * pente_pct / 100  # ml/kg/min
    return round(vo2 * poids_kg / 1000 * 5 * minutes)  # ≈ 5 kcal par litre d'O2


def corriger(data: bytes, distance_km: float, calories: int | None, pas: int | None,
             pente: float, poids_defaut: float, taille_defaut: float) -> tuple[bytes, Resultat]:
    fit = FitFile(data)
    recs = fit.du_type(MSG_RECORD)
    sessions, laps = fit.du_type(MSG_SESSION), fit.du_type(MSG_LAP)
    if not recs or not sessions:
        raise ValueError("Le fichier ne contient pas de points ou de session.")

    profil = fit.du_type(MSG_USER_PROFILE)
    poids = poids_defaut
    taille = taille_defaut
    if profil:
        w, t = fit.lire(profil[0], 4), fit.lire(profil[0], 3)  # poids (×10), taille (×100)
        if w and w != 0xFFFF:
            poids = w / 10
        if t and t != 0xFF:
            taille = t / 100

    intervalles = secondes_actives(fit)
    actif_total = sum(b - a for a, b in intervalles) or 1

    def actif_jusqua(ts: int) -> float:
        s = 0
        for a, b in intervalles:
            if ts >= b:
                s += b - a
            elif ts > a:
                s += ts - a
        return s

    distance_m = distance_km * 1000
    vitesse = distance_m / actif_total  # m/s
    minutes = actif_total / 60
    if pas is None:
        pas = round(distance_m / (0.415 * taille))  # longueur de pas de marche ≈ 0,415 × taille
    spm = pas / minutes  # pas par minute
    cadence = spm / 2    # le FIT stocke des foulées (un pied)
    cad_ent, cad_frac = int(cadence), (cadence - int(cadence)) * 128

    distance_avant = (fit.lire(sessions[0], SES["total_distance"]) or 0) / 100
    calories_avant = fit.lire(sessions[0], SES["total_calories"]) or 0
    if calories is None:
        calories = calories_acsm(poids, vitesse * 3.6, pente, minutes)

    for i, r in enumerate(recs):
        actif = actif_jusqua(r.timestamp)
        en_marche = any(a <= r.timestamp < b for a, b in intervalles) and i < len(recs) - 1
        fit.ecrire(r, REC["distance"], actif * vitesse * 100)
        fit.ecrire(r, REC["enhanced_speed"], (vitesse if en_marche else 0) * 1000)
        fit.ecrire(r, REC["speed"], (vitesse if en_marche else 0) * 1000)
        fit.ecrire(r, REC["cadence"], cad_ent if en_marche else 0)
        fit.ecrire(r, REC["fractional_cadence"], cad_frac if en_marche else 0)
    # le dernier point porte la distance totale exacte
    fit.ecrire(recs[-1], REC["distance"], distance_m * 100)

    for msgs, champs in ((sessions, SES), (laps, LAP)):
        for m in msgs:
            fit.ecrire(m, champs["total_distance"], distance_m * 100)
            fit.ecrire(m, champs["total_calories"], calories)
            fit.ecrire(m, champs["total_strides"], pas / 2)
            for k in ("avg_speed", "max_speed", "enhanced_avg_speed", "enhanced_max_speed"):
                fit.ecrire(m, champs[k], vitesse * 1000)
            for k in ("avg_cadence", "max_cadence"):
                fit.ecrire(m, champs[k], cad_ent)
            for k in ("avg_fractional_cadence", "max_fractional_cadence"):
                fit.ecrire(m, champs[k], cad_frac)

    debut = datetime.fromtimestamp(recs[0].timestamp + FIT_EPOCH)
    return fit.octets(), Resultat(debut, minutes, distance_avant, distance_m,
                                  calories_avant, calories, pas, vitesse * 3.6)


# ---------------------------------------------------------------------- Garmin

MARCHES = ("walking", "indoor_walking", "treadmill_walking")
JSON = False  # mode utilisé par l'app de la barre de menus


def emettre(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def echec(message: str) -> None:
    if JSON:
        emettre({"erreur": message})
        sys.exit(1)
    sys.exit(message)


def connexion(interactive: bool = True):
    """Jetons enregistrés d'abord ; sinon connexion unique par email et mot de passe."""
    from garminconnect import Garmin

    TOKENS.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        g = Garmin()
        g.login(str(TOKENS))
        return g
    except Exception:  # noqa: BLE001
        if not interactive:
            return None

    if JSON:  # l'app envoie email puis mot de passe sur l'entrée standard
        email = sys.stdin.readline().strip()
        mot_de_passe = sys.stdin.readline().rstrip("\n")

        def mfa() -> str:
            emettre({"mfa": True})
            return sys.stdin.readline().strip()
    else:
        from getpass import getpass

        print("Connexion à Garmin Connect (une seule fois, les jetons sont ensuite gardés).")
        email = input("  Email Garmin : ").strip()
        mot_de_passe = getpass("  Mot de passe : ")

        def mfa() -> str:
            return input("  Code de vérification reçu : ").strip()

    g = Garmin(email, mot_de_passe, prompt_mfa=mfa)
    try:
        g.login(str(TOKENS))
    except Exception as e:  # noqa: BLE001
        echec(f"Connexion à Garmin impossible : {e}")
    return g


def resume_activite(a: dict, nom_corrige: str) -> dict:
    duree = a.get("duration") or 0
    return {
        "id": a["activityId"],
        "nom": a.get("activityName") or "",
        "debut": a.get("startTimeLocal") or "",
        "duree_min": round(duree / 60),
        "deplacement_min": round((a.get("movingDuration") or 0) / 60),
        "distance_km": round((a.get("distance") or 0) / 1000, 2),
        "calories": round(a.get("calories") or 0),
        "pas": a.get("steps"),
        "corrigee": (a.get("activityName") or "") == nom_corrige,
    }


def marches(g, n: int = 20) -> list[dict]:
    return [a for a in g.get_activities(0, n) if a.get("activityType", {}).get("typeKey") in MARCHES]


def derniere_marche(g, id_voulu: int | None) -> dict:
    liste = marches(g)
    if id_voulu:
        for a in liste:
            if a["activityId"] == id_voulu:
                return a
        return {"activityId": id_voulu, "activityName": "?", "startTimeLocal": "?"}
    if not liste:
        echec("Aucune marche dans les 20 dernières activités.")
    return liste[0]


def telecharger_fit(g, activity_id: int) -> bytes:
    zipped = g.download_activity(str(activity_id), dl_fmt=g.ActivityDownloadFormat.ORIGINAL)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        nom = next(n for n in z.namelist() if n.lower().endswith(".fit"))
        return z.read(nom)


def retrouver_envoi(g, reponse, debut_local: str, ancien_id: int) -> int | None:
    """Id de l'activité créée par l'envoi (réponse de Garmin, sinon par l'heure de début)."""
    try:
        corps = reponse.json() if hasattr(reponse, "json") else reponse
        succes = corps["detailedImportResult"]["successes"]
        if succes and succes[0].get("internalId"):
            return int(succes[0]["internalId"])
    except Exception:  # noqa: BLE001
        pass
    for _ in range(10):  # Garmin traite parfois l'envoi en différé
        for a in g.get_activities(0, 10):
            # l'activité supprimée reste parfois listée quelques secondes : l'écarter
            if a.get("startTimeLocal") == debut_local and int(a["activityId"]) != int(ancien_id):
                return int(a["activityId"])
        time.sleep(3)
    return None


# ------------------------------------------------------------------------ main

def demander_float(question: str) -> float:
    while True:
        rep = input(question).strip().replace(",", ".")
        try:
            return float(rep)
        except ValueError:
            print("  Un nombre, par exemple 4.5")


def resume(r: Resultat) -> str:
    return (f"  Début      : {r.debut:%d/%m/%Y %H:%M}  ({r.duree_min:.0f} min de marche)\n"
            f"  Distance   : {r.distance_avant_m / 1000:.2f} km  →  {r.distance_m / 1000:.2f} km"
            f"  ({r.vitesse_kmh:.1f} km/h)\n"
            f"  Calories   : {r.calories_avant}  →  {r.calories} kcal\n"
            f"  Pas        : {r.pas}")


def resultat_json(r: Resultat) -> dict:
    return {
        "duree_min": round(r.duree_min), "vitesse_kmh": round(r.vitesse_kmh, 1),
        "distance_avant_km": round(r.distance_avant_m / 1000, 2), "distance_km": round(r.distance_m / 1000, 2),
        "calories_avant": r.calories_avant, "calories": r.calories, "pas": r.pas,
    }


def main() -> None:
    global JSON
    p = argparse.ArgumentParser(description="Corrige une marche sur tapis enregistrée par Garmin.")
    p.add_argument("--distance", type=float, help="distance réelle en km (lue sur le tapis)")
    p.add_argument("--calories", type=int, help="calories à retenir (sinon estimation ACSM)")
    p.add_argument("--pas", type=int, help="nombre de pas (sinon estimé d'après la taille)")
    p.add_argument("--pente", type=float, default=0.0, help="pente du tapis en %% (défaut 0)")
    p.add_argument("--id", type=int, help="id de l'activité Garmin (défaut : dernière marche)")
    p.add_argument("--fichier", type=Path, help="corrige un FIT local sans toucher à Garmin")
    p.add_argument("--nom", default="Tapis de marche", help="nom donné à l'activité renvoyée")
    p.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    p.add_argument("--poids", type=float, default=75.0, help="poids si absent du profil Garmin du FIT (kg)")
    p.add_argument("--taille", type=float, default=1.75, help="taille si absente du profil Garmin du FIT (m)")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--etat", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--connexion", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--apercu", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()
    JSON = a.json

    SAUVEGARDES.mkdir(parents=True, exist_ok=True)

    if a.fichier:
        distance = a.distance or demander_float("Distance affichée par le tapis (km) : ")
        data, r = corriger(a.fichier.read_bytes(), distance, a.calories, a.pas, a.pente, a.poids, a.taille)
        sortie = a.fichier.with_name(a.fichier.stem + "_corrige.fit")
        sortie.write_bytes(data)
        if JSON:
            emettre({"resultat": resultat_json(r), "fichier": str(sortie)})
        else:
            print(resume(r))
            print(f"\nFichier corrigé : {sortie}")
        return

    if a.connexion:
        connexion(interactive=True)
        emettre({"connecte": True})
        return

    if a.etat:
        g = connexion(interactive=False)
        if g is None:
            emettre({"connecte": False})
            return
        emettre({"connecte": True, "marches": [resume_activite(m, a.nom) for m in marches(g)[:6]]})
        return

    g = connexion(interactive=not JSON)
    if g is None:
        echec("Pas connecté à Garmin.")
    try:
        act = derniere_marche(g, a.id)
        aid = act["activityId"]
        if not JSON:
            print(f"Activité {aid} : {act.get('activityName')} du {act.get('startTimeLocal')}")
        original = telecharger_fit(g, aid)

        distance = a.distance or demander_float("Distance affichée par le tapis (km) : ")
        data, r = corriger(original, distance, a.calories, a.pas, a.pente, a.poids, a.taille)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        echec(f"Lecture de l'activité impossible : {e}")

    if a.apercu:
        emettre({"resultat": resultat_json(r)})
        return
    if not JSON:
        print(resume(r))

    horodatage = f"{r.debut:%Y-%m-%d_%H%M}"
    sauvegarde = SAUVEGARDES / f"{horodatage}_{aid}_original.fit"
    corrige = SAUVEGARDES / f"{horodatage}_{aid}_corrige.fit"
    sauvegarde.write_bytes(original)
    corrige.write_bytes(data)
    if not JSON:
        print(f"\nOriginal sauvegardé : {sauvegarde}")

    if not a.oui and input("\nSupprimer l'activité sur Garmin et envoyer la version corrigée ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
        print(f"Rien n'a été modifié sur Garmin. Fichier corrigé : {corrige}")
        return

    debut_local = act.get("startTimeLocal") or ""
    g.delete_activity(str(aid))
    try:
        reponse = g.upload_activity(str(corrige))
    except Exception as e:  # noqa: BLE001
        echec(f"Échec de l'envoi ({e}). L'original est supprimé de Garmin : "
              f"réimporte {corrige} (ou {sauvegarde}) depuis connect.garmin.com.")

    nouvel_id = retrouver_envoi(g, reponse, debut_local, aid)
    nom_ok = False
    if nouvel_id and a.nom:
        for _ in range(5):  # Garmin peut refuser tant que l'import n'est pas fini
            try:
                g.set_activity_name(str(nouvel_id), a.nom)
                nom_ok = g.get_activity(nouvel_id).get("activityName") == a.nom
            except Exception:  # noqa: BLE001
                nom_ok = False
            if nom_ok:
                break
            time.sleep(3)
    if JSON:
        emettre({"resultat": resultat_json(r), "id": nouvel_id, "sauvegarde": str(sauvegarde)})
    else:
        print(f"C'est fait : activité {nouvel_id or '?'} sur Garmin.")


if __name__ == "__main__":
    main()
