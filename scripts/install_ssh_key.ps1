# Installe la clé SSH publique de ce PC sur l'Ultimaker 3 (le mot de passe root est demandé une fois).
$hostName = "ultimakersystem-ccbdd3009d9d.local"
$opts = @("-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa", "-o", "MACs=hmac-sha1")
Get-Content "$env:USERPROFILE\.ssh\id_rsa.pub" | ssh @opts "root@$hostName" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo CLE_INSTALLEE"
Write-Host "Test sans mot de passe :"
ssh @opts -o BatchMode=yes "root@$hostName" "echo CONNEXION_PAR_CLE_OK"
