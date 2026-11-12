# Plan du projet — Interface d'automatisation Raman

## 1. Matériel et accès logiciel

| Élément | Contrôle | Accès depuis Python |
|---|---|---|
| Laser 514.5 / 488 nm | **Manuel** (alim, clé, obturateur, réglage de raie, filtre de sonde) | Aucun → check-lists bloquantes dans l'interface |
| Spectromètre + caméra | LightField (Teledyne PI) | pythonnet + SDK Automation (`PrincetonInstruments.LightField.AutomationV5`) |
| Correction de réponse spectrale | IntelliCal, appliquée dans LightField | État lu et stocké dans les métadonnées |
| Wattmètre Thorlabs PM100D | USB | `PM100D_64.dll` via ctypes (NI-VISA présent : `visa64.dll`) |
| Platine XYZ (Ultimaker 3) | Linux embarqué, mode développeur | OpenSSH (clé) + pont sur l'imprimante → D-Bus `debugSendGCode` de griffin |

Environnement : conda **`raman`** (Python 3.14 64 bit, PySide6, pyqtgraph, pythonnet, pyvisa, paramiko, pyyaml, numpy, scipy, h5py, matplotlib).

## 2. Architecture

```
raman_auto/
├── config/
│   └── config.yaml        # hôte Ultimaker, limites XYZ, Z de sécurité, raies laser, chemins de sauvegarde
├── drivers/
│   ├── base.py            # interfaces abstraites Spectrometer / PowerMeter / Stage
│   ├── lightfield.py      # capture → (axe nm, axe cm⁻¹, spectre), exposition, accumulations, état IntelliCal
│   ├── pm100d.py          # ctypes sur PM100D_64.dll : init, λ de correction, zéro, moyennage, puissance
│   ├── ultimaker.py       # SSH + D-Bus → G-code : G0, G28, M114, M400, limites logicielles, move_precise
│   └── sim/               # simulateurs (spot gaussien, pic Si 520.7 cm⁻¹, bruit) pour développer hors labo
├── core/
│   ├── session.py         # raie laser active (514.5 / 488), filtre confirmé, puissance de référence
│   ├── tasks.py           # séquences en QThread : pause / arrêt / reprise, sauvegarde point par point
│   ├── analysis.py        # ajustement erf, caustique, ajustement de pic, rayons cosmiques, stats
│   └── storage.py         # HDF5 : spectres, positions, puissance, raie, exposition, IntelliCal, horodatage
└── gui/
    ├── main_window.py     # connexions, état des appareils, journal
    └── tabs/              # Live · Puissance · Knife-edge · Focus Z · Mapping · Répétitions
```

Principes :
- **LightField dans un thread dédié** : `Automation` est créé et utilisé dans le même thread (.NET/pythonnet).
- **Laser manuel** : au démarrage de la session, choisir la raie et confirmer « laser réglé + filtre de sonde installé ». La raie choisie règle la correction λ du PM100D et le calcul du décalage Raman. Pour un noir (dark), l'interface demande de fermer l'obturateur.
- **Sécurité de la platine** : limites logicielles XYZ, montée à Z de sécurité avant chaque déplacement XY, approche toujours dans le même sens (jeu mécanique), `M400` avant chaque acquisition (fin de mouvement garantie).
- Chaque spectre est enregistré avec la **puissance de référence** de la session, ce qui permet de le normaliser en counts / (mW·s).

## 3. Phases

### Phase 0 — Validation matériel (scripts autonomes)
- **LightField** : ouvrir une expérience, régler l'exposition, capturer, obtenir l'axe en longueur d'onde, vérifier que l'état IntelliCal est lisible.
- **PM100D** : énumérer les appareils, init, régler λ = 514.5 / 488 nm, lire la puissance à ~10 Hz.
- **Ultimaker** : G-code par D-Bus (`debugSendGCode`), griffin reste actif ; le port série direct est inutilisable (protocole Ultimaker avec séquence + checksum).

**Résultats du 2026-11-09**
- Platine : homing OK (après G28 : X = 0, **Y = 215**, Z = 210). ~70 ms par commande ; `M400` attend bien la fin du mouvement.
- Le firmware ignore les mouvements de moins de 5 pas (~62 µm en XY). Avec `move_precise` (pré-position + approche du même côté), les compteurs suivent des pas de 12,5 µm en XY et de 10 µm en Z. La précision mécanique réelle (jeu) reste à mesurer au knife-edge.
- LightField : PIXIS 100B eXcelon (1340 × 100) + SP-2-500i, capture et axe spectral OK, temps mort ~0,12 s par acquisition. Spectre avec signal laser non vérifié (mesure sans signal : niveau de base ~590 counts).
- PM100D : capteur S121C, lecture OK. Zéro et mesure avec laser à faire, faisceau contrôlé par l'opérateur.
- Reste : cote de contact plateau/sonde (`limits.z`), test avec laser sur Si, réglage IntelliCal (désactivé dans l'expérience par défaut ; à charger avec une expérience sauvegardée).

### Phase 1 — Drivers + simulateurs + config
Interfaces communes, implémentations réelles et simulées, tests unitaires sur les simulateurs.

### Phase 2 — Interface de base
Lancement : `python run_gui.py` (appareils réels) ou `python run_gui.py --sim` (simulation).

Fait : vue 2D de la platine avec la position de la sonde, dessin et édition des zones (rectangle, ligne, point), pas et nombre de points par zone, durée estimée, sauvegarde des zones, choix du réseau, centre, exposition, raie laser, acquisition d'un spectre, homing et déplacements manuels XYZ, journal.

Fait aussi : wattmètre (connexion, lecture toutes les 500 ms avec courbe sur 2 min, zéro avec confirmation, puissance de référence, longueur d'onde suivant la raie laser choisie) ; exécution des zones (voir phase 6).

Reste : spectre en direct (aperçu continu).

### Phase 3 — Mesure de puissance
- Capteur placé au plan de l'échantillon (à 7 mm de la sonde).
- Mesure moyennée et enregistrée comme puissance de référence de la session.
- Suivi dans le temps (préchauffage, dérive) avec graphique et export.
- Rappel de remesure (avant/après une longue carte) ; comparaison avec la session précédente.

### Phase 4 — Taille du spot (knife-edge)
Fait (2026-11-12), mode A au wattmètre : bouton « Knife-edge… » de l'interface (`raman_auto/knife/`,
`raman_auto/gui/knife_window.py`). Carte rectangulaire → ajustement d'un bord droit (position, orientation)
→ lignes perpendiculaires au bord, recentrées et adaptées au spot (~10 w, pas w/5 ≥ 12,5 µm) → une ligne par Z
→ caustique (z0, w0, zR, M²), second passage fin autour du minimum, fin au foyer. Option : réutiliser le bord
trouvé (pas de carte). Fichier `data/knife_edge/<date>/knife_<heure>.h5`. Validé en simulation (lame à 20°,
w0 = 30 µm, foyer 25 mm, zR = 0,4 mm : tout retrouvé à < 1 %) ; essai réel à faire.
Note : M114 ne donne la position qu'à 0,01 mm → la position utilisée est la cible arrondie au pas moteur
(12,5 µm) quand la lecture concorde ; sinon les profils sont faussés de quelques µm.
Essais réels au wattmètre (2026-11-12) : foyer Z ≈ 9,83 mm, w0 ≈ 64–68 µm (diamètre 1/e² ≈ 130 µm),
zR ≈ 0,30 mm (deux mesures concordantes à 0,02 mm près sur le foyer).

Fait (2026-11-12), mode B (bord de wafer de Si) : signal « Raman — pic du Si » (fenêtre 495–545 cm⁻¹) ou
« Raie laser résiduelle » (fenêtre −40 à 40 cm⁻¹), aire avec fond linéaire, image complète sommée sur les lignes,
réglages courants du spectromètre ; spectre de chaque point enregistré ; avertissement de saturation de la caméra ;
fenêtre vérifiée contre l'axe du spectromètre avant de commencer. Validé en simulation ; essai réel à faire.

**Mode A : lame de rasoir + PM100D (maintenant)**
- Lame fixée sur le plateau, capteur dessous ; la sonde balaie en X (puis en Y) à travers le bord.
- Ajustement `P(x) = P_off + P₀/2 · [1 − erf(√2 (x − x₀) / w)]` → rayon w à 1/e², diamètre 2w, FWHM.
- Balayage à plusieurs Z autour de 7 mm → **caustique** `w(z) = w₀ √(1 + ((z − z₀)/z_R)²)` → waist w₀, position du foyer z₀, longueur de Rayleigh z_R, pour chaque raie (488 et 514.5).
- Pas adaptatif : grossier pour trouver le bord, fin (≤ w/5) dans la transition.

**Mode B : wafer de silicium (fait, voir plus haut)**
- Balayage à travers un bord clivé du wafer, avec comme signal l'aire du pic Si 520.7 cm⁻¹ mesurée par le spectromètre.
- Donne la résolution effective excitation × collection (différente du spot laser seul) ; à comparer au mode A.

### Phase 5 — Optimisation de la hauteur (Z)
- Balayage Z grossier puis fin ; critère au choix : aire ou hauteur d'une bande, signal total, ou pic Si quand le wafer sert de support.
- Ajustement gaussien ou parabolique, puis retour au Z optimal (approche dans le même sens).
- **Plan d'inclinaison** : focus sur 3 ou 4 coins (pic Si du wafer support) → correction de Z automatique pendant le mapping.
- Option : refocus périodique pendant les longues cartes.

### Phase 6 — Mapping et acquisitions répétées
Fait (2026-11-12) : bouton « Lancer les zones » — toutes les zones dans l'ordre (rectangles en serpentin),
déplacement avec pré-position, image complète de la caméra (100 × 1340) à chaque point, sauvegarde HDF5
point par point dans `data/runs/<date>/<nom>_<heure>.h5` (format décrit dans `raman_auto/core/storage.py`),
check-list laser avant le départ, pause / reprise / arrêt entre deux points, points mesurés affichés sur la vue.
Par zone : case « Raman » (cochée par défaut, image complète) et case « Wattmètre » (lecture de puissance au
point, faite pendant l'exposition si les deux sont cochées) ; une zone « Wattmètre » seule ne prend pas d'image.
Validé en simulation ; essai réel à faire (plateau à Z = 20 lors du test : non lancé).

- **Mapping** : grille rectangulaire, balayage en serpentin ou ligne par ligne, liste de points ; carte en direct (intégrale ou position d'une bande) ; temps restant estimé ; sauvegarde point par point et reprise après interruption.
- **Répétitions** : N spectres par point (ou sur plusieurs points) → moyenne, écart-type, SNR, retrait des rayons cosmiques (comparaison entre répétitions), suivi de la puissance si le capteur est disponible.

### Phase 7 — Export, analyse, documentation
Fait (2026-11-12) : outil d'analyse séparé `python run_analyse.py [fichier.h5]` (aussi bouton « Analyse des
fichiers… » de l'interface, lancé dans un autre processus). Aucun appareil utilisé ; un fichier en cours d'écriture
peut être relu (« Recharger »). Code dans `raman_auto/analysis/`.
- Signal par point : puissance brute (`power_W`) ou fenêtre d'intégration Raman (aire, hauteur ou position du
  maximum ; bornes en cm⁻¹ ou nm ; fond linéaire optionnel ; lignes de caméra choisies ; division par la puissance).
- Vues : carte (rectangles en image, lignes et points en pastilles, échelle de couleur réglable), graphique
  (selon distance, X, Y, n° de point ou temps), spectre du point cliqué avec les fenêtres déplaçables, image caméra
  avec les lignes sommées.
- Export CSV (puissance + toutes les fenêtres) et PNG ; fenêtres mémorisées dans `data/analyse/fenetres.json`.

Reste : procédures pas à pas (changement de raie, mesure de puissance, knife-edge), mode d'emploi.

## 4. Risques et points ouverts
- **`debugSendGCode`** est une méthode de débogage de griffin : à revalider si le firmware de l'imprimante est mis à jour.
- **Résolution de la platine** vs taille du spot : pas de 12,5 µm en XY ; si le spot fait moins de ~50 µm, le knife-edge aura peu de points dans la transition (w/5 ≥ 12,5 µm → w ≳ 60 µm pour un échantillonnage confortable).
- **LightField** : l'automatisation ouvre sa propre instance → fermer LightField avant de lancer l'interface ; enregistrer une expérience LightField de référence (`.lfe`) par raie.
- **Changement de raie** : entièrement manuel ; le logiciel ne fait que vérifier la cohérence (raie choisie ↔ position du pic Si attendue).
