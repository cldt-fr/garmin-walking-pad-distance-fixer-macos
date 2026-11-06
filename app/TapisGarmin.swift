import AppKit
import ServiceManagement
import SwiftUI

// MARK: - Données

struct Marche: Identifiable, Hashable {
    let id: Int
    let nom: String
    let debut: String
    let dureeMin: Int
    let deplacementMin: Int
    let distanceKm: Double
    let calories: Int
    let corrigee: Bool

    init(_ d: [String: Any]) {
        id = d["id"] as? Int ?? 0
        nom = d["nom"] as? String ?? ""
        debut = d["debut"] as? String ?? ""
        dureeMin = d["duree_min"] as? Int ?? 0
        deplacementMin = d["deplacement_min"] as? Int ?? 0
        distanceKm = (d["distance_km"] as? NSNumber)?.doubleValue ?? 0
        calories = d["calories"] as? Int ?? 0
        corrigee = d["corrigee"] as? Bool ?? false
    }

    /// Signature du tapis : la montre n'a presque pas vu de déplacement.
    var aCorriger: Bool { !corrigee && dureeMin >= 10 && deplacementMin * 2 < dureeMin }

    var recente: Bool {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let d = f.date(from: debut) else { return false }
        return Date().timeIntervalSince(d) < 3 * 24 * 3600
    }

    /// « 2026-11-06 13:48:02 » → « 06/11 13:48 »
    var dateCourte: String {
        let p = debut.split(separator: " ")
        guard p.count == 2 else { return debut }
        let j = p[0].split(separator: "-")
        guard j.count == 3 else { return debut }
        return "\(j[2])/\(j[1]) \(p[1].prefix(5))"
    }

    var libelle: String {
        "\(dateCourte) · \(dureeMin) min · \(km(distanceKm))" + (corrigee ? " ✓" : "")
    }
}

struct Resultat {
    let dureeMin: Int
    let vitesse: Double
    let distanceAvant: Double
    let distance: Double
    let caloriesAvant: Int
    let calories: Int
    let pas: Int

    init?(_ d: [String: Any]?) {
        guard let d else { return nil }
        dureeMin = d["duree_min"] as? Int ?? 0
        vitesse = (d["vitesse_kmh"] as? NSNumber)?.doubleValue ?? 0
        distanceAvant = (d["distance_avant_km"] as? NSNumber)?.doubleValue ?? 0
        distance = (d["distance_km"] as? NSNumber)?.doubleValue ?? 0
        caloriesAvant = d["calories_avant"] as? Int ?? 0
        calories = d["calories"] as? Int ?? 0
        pas = d["pas"] as? Int ?? 0
    }
}

func km(_ v: Double) -> String {
    String(format: "%.2f km", v).replacingOccurrences(of: ".", with: ",")
}

func nombre(_ s: String) -> Double? {
    Double(s.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "."))
}

// MARK: - Appel du script Python

final class Script {
    static var uv: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        for p in ["\(home)/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "uv"
    }

    static var chemin: String {
        Bundle.main.path(forResource: "tapis", ofType: "py")
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Garmin-tapis/tapis.py").path
    }

    let process = Process()
    private let entree = Pipe()
    private let sortie = Pipe()
    private var tampon = Data()
    private var lignes: [[String: Any]] = []

    init(_ args: [String]) {
        process.executableURL = URL(fileURLWithPath: Script.uv)
        process.arguments = ["run", "--quiet", "--script", Script.chemin, "--json"] + args
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        process.environment = env
        process.standardInput = entree
        process.standardOutput = sortie
        process.standardError = FileHandle.nullDevice
    }

    func ecrire(_ ligne: String) {
        entree.fileHandleForWriting.write((ligne + "\n").data(using: .utf8)!)
    }

    /// Lance le script ; `surLigne` reçoit chaque objet JSON au fil de l'eau.
    func lancer(surLigne: @escaping @MainActor ([String: Any]) -> Void = { _ in }) async -> [[String: Any]] {
        await withCheckedContinuation { suite in
            sortie.fileHandleForReading.readabilityHandler = { [weak self] h in
                guard let self else { return }
                self.tampon.append(h.availableData)
                while let i = self.tampon.firstIndex(of: 0x0A) {
                    let ligne = self.tampon[self.tampon.startIndex..<i]
                    self.tampon.removeSubrange(self.tampon.startIndex...i)
                    if let obj = try? JSONSerialization.jsonObject(with: ligne) as? [String: Any] {
                        self.lignes.append(obj)
                        Task { @MainActor in surLigne(obj) }
                    }
                }
            }
            process.terminationHandler = { [weak self] _ in
                guard let self else { return }
                self.sortie.fileHandleForReading.readabilityHandler = nil
                let reste = self.sortie.fileHandleForReading.readDataToEndOfFile()
                for l in reste.split(separator: 0x0A) {
                    if let obj = try? JSONSerialization.jsonObject(with: l) as? [String: Any] {
                        self.lignes.append(obj)
                    }
                }
                suite.resume(returning: self.lignes)
            }
            do { try process.run() } catch {
                suite.resume(returning: [["erreur": "Impossible de lancer uv (\(Script.uv)) : \(error.localizedDescription)"]])
            }
        }
    }
}

// MARK: - Modèle

@MainActor
final class Modele: ObservableObject {
    enum Etat { case chargement, deconnecte, pret }

    @Published var etat: Etat = .chargement
    @Published var marches: [Marche] = []
    @Published var selection: Int?
    @Published var distance = ""
    @Published var calories = ""
    @Published var pente = ""
    @Published var apercu: Resultat?
    @Published var occupe = false
    @Published var message: String?
    @Published var erreur: String?

    @Published var email = ""
    @Published var motDePasse = ""
    @Published var mfaDemande = false
    @Published var codeMfa = ""
    private var connexionEnCours: Script?

    private var minuterie: Timer?
    @Published var auDemarrage = SMAppService.mainApp.status == .enabled

    init() {
        Task { await actualiser() }
        minuterie = Timer.scheduledTimer(withTimeInterval: 30 * 60, repeats: true) { [weak self] _ in
            Task { await self?.actualiser() }
        }
    }

    var marcheChoisie: Marche? { marches.first { $0.id == selection } }
    /// L'icône ne signale que les séances récentes (les anciennes corrigées à la main gardent un temps en mouvement nul).
    var aCorriger: Bool { marches.contains { $0.aCorriger && $0.recente } }

    func actualiser() async {
        guard !occupe else { return }
        occupe = true
        defer { occupe = false }
        let r = await Script(["--etat"]).lancer().last ?? [:]
        if let e = r["erreur"] as? String { erreur = e; return }
        guard r["connecte"] as? Bool == true else { etat = .deconnecte; return }
        let liste = (r["marches"] as? [[String: Any]] ?? []).map(Marche.init)
        marches = liste
        if selection == nil || !liste.contains(where: { $0.id == selection }) {
            selection = (liste.first { $0.aCorriger && $0.recente } ?? liste.first)?.id
        }
        etat = .pret
        erreur = nil
    }

    func connecter() {
        let s = Script(["--connexion"])
        connexionEnCours = s
        occupe = true
        erreur = nil
        let email = email, mdp = motDePasse
        // le tube garde email et mot de passe jusqu'à ce que le script les lise
        s.ecrire(email)
        s.ecrire(mdp)
        Task {
            let fin = await s.lancer { [weak self] obj in
                if obj["mfa"] as? Bool == true { self?.mfaDemande = true }
            }
            connexionEnCours = nil
            mfaDemande = false
            codeMfa = ""
            occupe = false
            if let e = fin.last?["erreur"] as? String { erreur = e; return }
            motDePasse = ""
            await actualiser()
        }
    }

    func envoyerCode() {
        connexionEnCours?.ecrire(codeMfa.trimmingCharacters(in: .whitespaces))
        mfaDemande = false
    }

    private func argumentsCorrection() -> [String]? {
        guard let m = marcheChoisie else { erreur = "Choisis une marche."; return nil }
        guard let d = nombre(distance), d > 0 else { erreur = "Indique la distance du tapis, en km."; return nil }
        var args = ["--id", String(m.id), "--distance", String(d)]
        if !calories.trimmingCharacters(in: .whitespaces).isEmpty {
            guard let c = nombre(calories) else { erreur = "Les calories doivent être un nombre."; return nil }
            args += ["--calories", String(Int(c))]
        }
        if !pente.trimmingCharacters(in: .whitespaces).isEmpty {
            guard let p = nombre(pente) else { erreur = "La pente doit être un nombre."; return nil }
            args += ["--pente", String(p)]
        }
        return args
    }

    func calculerApercu() async {
        erreur = nil
        message = nil
        guard let args = argumentsCorrection() else { return }
        occupe = true
        defer { occupe = false }
        let r = await Script(args + ["--apercu"]).lancer().last ?? [:]
        if let e = r["erreur"] as? String { erreur = e; return }
        apercu = Resultat(r["resultat"] as? [String: Any])
    }

    func remplacer() async {
        erreur = nil
        guard let args = argumentsCorrection() else { return }
        occupe = true
        let r = await Script(args + ["--oui"]).lancer().last ?? [:]
        occupe = false
        if let e = r["erreur"] as? String { erreur = e; return }
        apercu = nil
        distance = ""
        calories = ""
        message = "Activité corrigée et renvoyée sur Garmin."
        selection = nil
        await actualiser()
    }

    func reinitialiserApercu() { apercu = nil; message = nil }
}

// MARK: - Vues

struct Vue: View {
    @EnvironmentObject var m: Modele

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: "figure.walk").foregroundStyle(.tint)
                Text("Tapis de marche").font(.headline)
                Spacer()
                if m.occupe { ProgressView().controlSize(.small) }
                Button { Task { await m.actualiser() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).disabled(m.occupe).help("Actualiser")
            }

            switch m.etat {
            case .chargement:
                Text("Lecture de Garmin Connect…").foregroundStyle(.secondary)
            case .deconnecte:
                connexion
            case .pret:
                correction
            }

            if let e = m.erreur {
                Label(e, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
            if let msg = m.message {
                Label(msg, systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.callout)
            }

            Divider()
            HStack {
                Toggle("Ouvrir au démarrage", isOn: $m.auDemarrage)
                    .toggleStyle(.checkbox).font(.callout)
                    .onChange(of: m.auDemarrage) { _, actif in
                        do {
                            if actif { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                        } catch { m.erreur = "Ouverture au démarrage : \(error.localizedDescription)" }
                    }
                Spacer()
                Button("Sauvegardes") {
                    NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser
                        .appendingPathComponent("Garmin-tapis/sauvegardes"))
                }.buttonStyle(.borderless).font(.callout)
                Button("Quitter") { NSApp.terminate(nil) }.buttonStyle(.borderless).font(.callout)
            }
        }
        .padding(14)
        .frame(width: 340)
    }

    private var connexion: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Connecte-toi une fois à Garmin Connect ; la connexion est ensuite gardée sur ce Mac.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if m.mfaDemande {
                TextField("Code de vérification reçu", text: $m.codeMfa)
                    .textFieldStyle(.roundedBorder).onSubmit { m.envoyerCode() }
                Button("Valider le code") { m.envoyerCode() }.keyboardShortcut(.defaultAction)
            } else {
                TextField("Email Garmin", text: $m.email).textFieldStyle(.roundedBorder)
                SecureField("Mot de passe", text: $m.motDePasse).textFieldStyle(.roundedBorder)
                    .onSubmit { m.connecter() }
                Button("Se connecter") { m.connecter() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(m.occupe || m.email.isEmpty || m.motDePasse.isEmpty)
            }
        }
    }

    private var correction: some View {
        VStack(alignment: .leading, spacing: 10) {
            if m.marches.isEmpty {
                Text("Aucune marche parmi tes 20 dernières activités.").foregroundStyle(.secondary)
            } else {
                Picker("Marche", selection: $m.selection) {
                    ForEach(m.marches) { a in Text(a.libelle).tag(Optional(a.id)) }
                }
                .labelsHidden()
                .onChange(of: m.selection) { _, _ in m.reinitialiserApercu() }

                if let a = m.marcheChoisie {
                    Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
                        GridRow { Text("Enregistré").foregroundStyle(.secondary); Text("\(km(a.distanceKm)) · \(a.calories) kcal") }
                        GridRow { Text("Durée").foregroundStyle(.secondary); Text("\(a.dureeMin) min, dont \(a.deplacementMin) en mouvement") }
                    }.font(.callout)
                    if a.corrigee {
                        Label("Déjà corrigée", systemImage: "checkmark.seal").font(.callout).foregroundStyle(.green)
                    }
                }

                Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 6) {
                    GridRow {
                        Text("Distance du tapis")
                        TextField("km", text: $m.distance).textFieldStyle(.roundedBorder).frame(width: 90)
                            .onChange(of: m.distance) { _, _ in m.apercu = nil }
                    }
                    GridRow {
                        Text("Calories du tapis")
                        TextField("auto", text: $m.calories).textFieldStyle(.roundedBorder).frame(width: 90)
                            .onChange(of: m.calories) { _, _ in m.apercu = nil }
                    }
                    GridRow {
                        Text("Pente (%)")
                        TextField("0", text: $m.pente).textFieldStyle(.roundedBorder).frame(width: 90)
                            .onChange(of: m.pente) { _, _ in m.apercu = nil }
                    }
                }.font(.callout)

                if let r = m.apercu {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Distance : \(km(r.distanceAvant)) → \(km(r.distance)) (\(String(format: "%.1f", r.vitesse).replacingOccurrences(of: ".", with: ",")) km/h)")
                        Text("Calories : \(r.caloriesAvant) → \(r.calories) kcal")
                        Text("Pas : \(r.pas)")
                    }
                    .font(.callout)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))

                    Text("L'activité sera supprimée de Garmin puis renvoyée corrigée. L'original est gardé dans les sauvegardes.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Annuler") { m.reinitialiserApercu() }
                        Spacer()
                        Button("Remplacer sur Garmin") { Task { await m.remplacer() } }
                            .keyboardShortcut(.defaultAction).disabled(m.occupe)
                    }
                } else {
                    HStack {
                        Spacer()
                        Button("Aperçu de la correction") { Task { await m.calculerApercu() } }
                            .keyboardShortcut(.defaultAction).disabled(m.occupe || m.distance.isEmpty)
                    }
                }
            }
        }
    }
}

@main
struct TapisGarminApp: App {
    @StateObject private var modele = Modele()

    var body: some Scene {
        MenuBarExtra {
            Vue().environmentObject(modele)
        } label: {
            Image(systemName: modele.aCorriger ? "figure.walk.circle.fill" : "figure.walk")
        }
        .menuBarExtraStyle(.window)
    }
}
