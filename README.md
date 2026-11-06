# tapis-garmin

Corrige une marche sur tapis enregistrée par la montre Garmin (distance, vitesse, pas, calories),
puis remplace l'activité sur Garmin Connect. Tout le reste du fichier d'origine (fréquence
cardiaque, effet d'entraînement…) est conservé.

## App de la barre de menus

L'app « Tapis Garmin » (`~/Applications/Tapis Garmin.app`) met un bonhomme qui marche dans la barre de menus.
Il devient un rond plein quand une marche de la montre n'a presque pas de déplacement : c'est une séance de tapis à corriger.
Clique dessus, choisis la marche, tape les km du tapis (et ses calories si tu veux), « Aperçu », puis « Remplacer sur Garmin ».
La case « Ouvrir au démarrage » la relance à chaque ouverture de session.

Recompiler après une modification : `app/build.sh` (le script `tapis.py` est copié dans l'app).

## Utilisation en ligne de commande

```bash
~/Garmin-tapis/tapis.py
```

1. récupère ta dernière marche sur Garmin Connect ;
2. demande la distance affichée par le tapis ;
3. affiche avant → après et demande confirmation ;
4. supprime l'activité d'origine et envoie la version corrigée, nommée « Tapis de marche ».

La première fois, l'outil demande l'email et le mot de passe Garmin (et le code de vérification) ;
les jetons sont ensuite gardés dans `~/Garmin-tapis/jetons`.
Chaque original est sauvegardé dans `~/Garmin-tapis/sauvegardes/` avant toute suppression.

Options : `--distance 4.5`, `--calories 350` (valeur du tapis), `--pas 7000`, `--pente 2`,
`--id <activité>`, `--oui` (sans confirmation), `--fichier act.fit` (hors ligne, n'écrit que le FIT).

## Calculs

- Distance répartie régulièrement sur le temps où le chrono tournait (pauses respectées).
- Pas estimés avec une longueur de pas de 0,415 × la taille du profil Garmin.
- Calories sans `--calories` : équation ACSM de la marche (repos compris), poids du profil Garmin.
