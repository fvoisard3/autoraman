# Phase 0 — procédure de test du matériel

Tous les scripts se lancent depuis `C:\controle_raman` avec le Python de l'environnement `raman` :

```powershell
cd C:\controle_raman
$py = "C:\Users\admin\.conda\envs\raman\python.exe"
```

Les résultats sont écrits dans `data\phase0\<appareil>\<date>\`. Chaque étape qui bouge la machine
ou qui suppose un état du laser demande une confirmation `[o/N]`
(`$env:RAMAN_AUTO_CONFIRM='1'` répond oui partout : à éviter pour le zéro du wattmètre, qui exige un
faisceau réellement bloqué).

Résultats du 2026-11-09 : voir la phase 0 dans `PLAN.md`.

## 1. Wattmètre PM100D (≈ 15 min)

Fermer **Optical Power Meter Utility**, brancher le PM100D en USB.

```powershell
& $py scripts\test_pm100d.py --list                                   # doit afficher USB0::0x1313::0x8078::...
& $py scripts\test_pm100d.py --wavelength 514.5 --zero --duration 60  # zéro (obturateur fermé) puis 1 min
& $py scripts\test_pm100d.py --wavelength 514.5 --duration 900 --rate 2   # dérive sur 15 min (optionnel)
```

À noter : puissance au plan de l'échantillon, bruit (σ/P), dérive en %/h.

## 2. LightField (≈ 15 min)

**Fermer LightField** (le script ouvre sa propre instance). Si vous avez une expérience sauvegardée
pour le Raman, donner son nom avec `--experiment`.

```powershell
& $py scripts\test_lightfield.py --exposure 1000 --n 5 --laser 514.5
& $py scripts\test_lightfield.py --experiment "NomDeVotreExperience" --n 5
```

À vérifier :
- la liste des appareils (caméra + spectromètre) et `Prêt : True`
- l'axe spectral en nm / cm⁻¹ correspond à ce que LightField affiche
- l'état IntelliCal (`intensity_calibration_enabled`) est bien lu
- le **temps mort** par acquisition (important pour estimer la durée des cartes)
- un échantillon de Si : pic à ~520 cm⁻¹ sur la figure PNG

## 3. Ultimaker 3

Connexion : câble Ethernet direct sur la carte « Ethernet 4 » du PC (le nom `.local` se résout
en IPv6 local). Le G-code passe par D-Bus (`debugSendGCode` du service griffin.printer) :
griffin reste actif, rien n'est arrêté sur l'imprimante.

### 3a. Clé SSH (fait le 2026-11-09)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_ssh_key.ps1   # mot de passe root demandé une fois
```

### 3b. Exploration (lecture seule, fait le 2026-11-09)

```powershell
& $py scripts\explore_ultimaker.py
```

Constats : Debian 8, Python 3.7, firmware 5.3.0 ; le contrôleur (/dev/ttyS1, 250 000 bauds)
parle un protocole Ultimaker spécifique (séquence + checksum) géré par griffin → accès par D-Bus.
Le serveur SSH coupe les commandes de plus de ~1000 caractères (le pont est copié par stdin).

### 3c. Test de mouvement

Axe Z : Z = 0 → plateau proche de la sonde ; G28 → X = Y = 0 et plateau en bas (Z max), sans
risque de collision. `limits.z` (config.yaml) empêche de descendre sous 20 mm en attendant de
mesurer la cote de contact.

```powershell
& $py scripts\test_ultimaker_motion.py --info-only          # connexion + firmware + position, sans mouvement
& $py scripts\test_ultimaker_motion.py --home --axis x      # homing (G28) puis tests sur X
& $py scripts\test_ultimaker_motion.py --axis y
& $py scripts\test_ultimaker_motion.py --axis z --travel 1  # petite course en Z
```

À noter : latence d'un déplacement, plus petit pas réellement exécuté (colonne compteur),
valeur de Z après homing (course totale).

## Fichiers

| Fichier | Rôle |
|---|---|
| `config/config.yaml` | Réglages du banc (hôte, limites, raies laser) |
| `raman_auto/drivers/lightfield.py` | Pilote LightField (pythonnet) |
| `raman_auto/drivers/pm100d.py` | Pilote PM100D (ctypes sur PM100D_64.dll) |
| `raman_auto/drivers/ultimaker.py` | Pilote de la platine (SSH + pont D-Bus) |
| `raman_auto/remote/um3_bridge.py` | Pont G-code exécuté sur l'imprimante (D-Bus → griffin) |
| `scripts/install_ssh_key.ps1` | Installation de la clé SSH sur l'imprimante |
| `scripts/*.py` | Scripts de test de la phase 0 |
