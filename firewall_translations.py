from translations import LANGUAGES

_RAW_PLACEHOLDER = ("e.g. ufw allow 22/tcp  OR  sudo ufw default reject incoming  OR  "
                    "firewall-cmd --permanent --add-port=80/tcp")
_RAW_UNKNOWN = ("'{rule}' could not be interpreted. Use ufw syntax (e.g. 'ufw allow 22/tcp') or "
                "firewall-cmd syntax (e.g. 'firewall-cmd --add-port=80/tcp'), or use 'Add Rule'.")
_BAD_ADDRESS = "'{value}' is not a valid IP address or network (e.g. 192.168.0.0/24)."
_BAD_PORT = ("'{value}' is not a valid port, port range or service name "
             "(e.g. 22, 6000:6007, 80,443 or ssh).")
_SKIPPED = ("{n} rule(s) could not be imported because they use features this editor does not "
            "support (e.g. application profiles or routing).")

_NEW: dict[str, dict[str, str]] = {
    "Deutsch": {
        "Import Existing Rules": "Vorhandene Regeln importieren",
        "Interface:": "Schnittstelle:",
        "Interface": "Schnittstelle",
        "e.g. eth0": "z. B. eth0",
        _RAW_PLACEHOLDER:
            "z. B. ufw allow 22/tcp  ODER  sudo ufw default reject incoming  ODER  "
            "firewall-cmd --permanent --add-port=80/tcp",
        "Rule not recognised": "Regel nicht erkannt",
        _RAW_UNKNOWN:
            "'{rule}' konnte nicht interpretiert werden. Verwende die ufw-Syntax (z. B. 'ufw allow 22/tcp') "
            "oder die firewall-cmd-Syntax (z. B. 'firewall-cmd --add-port=80/tcp') oder nutze 'Regel hinzufügen'.",
        "Invalid Destination": "Ungültiges Ziel",
        _BAD_ADDRESS: "'{value}' ist keine gültige IP-Adresse und kein gültiges Netzwerk (z. B. 192.168.0.0/24).",
        "Invalid Port": "Ungültiger Port",
        _BAD_PORT:
            "'{value}' ist kein gültiger Port, Portbereich oder Dienstname (z. B. 22, 6000:6007, 80,443 oder ssh).",
        "Invalid Interface": "Ungültige Schnittstelle",
        "'{value}' is not a valid network interface name.":
            "'{value}' ist kein gültiger Name einer Netzwerkschnittstelle.",
        "Reading the current firewall rules requires administrator rights.":
            "Zum Auslesen der aktuellen Firewall-Regeln sind Administratorrechte erforderlich.",
        "No existing firewall rules were found.": "Es wurden keine vorhandenen Firewall-Regeln gefunden.",
        "{n} rule(s) found. Replace the current list or merge them into it?":
            "{n} Regel(n) gefunden. Aktuelle Liste ersetzen oder die Regeln zusammenführen?",
        "Replace": "Ersetzen",
        "Merge": "Zusammenführen",
        _SKIPPED:
            "{n} Regel(n) konnten nicht importiert werden, da sie Funktionen nutzen, die dieser Editor nicht "
            "unterstützt (z. B. Anwendungsprofile oder Routing).",
        "'{tool}' is not installed or not available.": "'{tool}' ist nicht installiert oder nicht verfügbar.",
        "The firewall query failed: {detail}": "Die Firewall-Abfrage ist fehlgeschlagen: {detail}",
        "unknown error": "unbekannter Fehler",
        "Unsupported firewall backend: {backend}": "Nicht unterstütztes Firewall-Backend: {backend}",
    },
    "Français": {
        "Import Existing Rules": "Importer les règles existantes",
        "Interface:": "Interface :",
        "Interface": "Interface",
        "e.g. eth0": "p. ex. eth0",
        _RAW_PLACEHOLDER:
            "p. ex. ufw allow 22/tcp  OU  sudo ufw default reject incoming  OU  "
            "firewall-cmd --permanent --add-port=80/tcp",
        "Rule not recognised": "Règle non reconnue",
        _RAW_UNKNOWN:
            "« {rule} » n'a pas pu être interprétée. Utilisez la syntaxe ufw (p. ex. 'ufw allow 22/tcp') "
            "ou la syntaxe firewall-cmd (p. ex. 'firewall-cmd --add-port=80/tcp'), ou utilisez 'Ajouter une règle'.",
        "Invalid Destination": "Destination invalide",
        _BAD_ADDRESS: "« {value} » n'est pas une adresse IP ou un réseau valide (p. ex. 192.168.0.0/24).",
        "Invalid Port": "Port invalide",
        _BAD_PORT:
            "« {value} » n'est pas un port, une plage de ports ou un nom de service valide "
            "(p. ex. 22, 6000:6007, 80,443 ou ssh).",
        "Invalid Interface": "Interface invalide",
        "'{value}' is not a valid network interface name.":
            "« {value} » n'est pas un nom d'interface réseau valide.",
        "Reading the current firewall rules requires administrator rights.":
            "La lecture des règles actuelles du pare-feu nécessite des droits d'administrateur.",
        "No existing firewall rules were found.": "Aucune règle de pare-feu existante n'a été trouvée.",
        "{n} rule(s) found. Replace the current list or merge them into it?":
            "{n} règle(s) trouvée(s). Remplacer la liste actuelle ou les fusionner avec elle ?",
        "Replace": "Remplacer",
        "Merge": "Fusionner",
        _SKIPPED:
            "{n} règle(s) n'ont pas pu être importées, car elles utilisent des fonctions non prises en charge "
            "par cet éditeur (p. ex. profils d'application ou routage).",
        "'{tool}' is not installed or not available.": "« {tool} » n'est pas installé ou n'est pas disponible.",
        "The firewall query failed: {detail}": "La requête au pare-feu a échoué : {detail}",
        "unknown error": "erreur inconnue",
        "Unsupported firewall backend: {backend}": "Moteur de pare-feu non pris en charge : {backend}",
    },
    "Español": {
        "Import Existing Rules": "Importar reglas existentes",
        "Interface:": "Interfaz:",
        "Interface": "Interfaz",
        "e.g. eth0": "p. ej. eth0",
        _RAW_PLACEHOLDER:
            "p. ej. ufw allow 22/tcp  O  sudo ufw default reject incoming  O  "
            "firewall-cmd --permanent --add-port=80/tcp",
        "Rule not recognised": "Regla no reconocida",
        _RAW_UNKNOWN:
            "No se pudo interpretar '{rule}'. Usa la sintaxis de ufw (p. ej. 'ufw allow 22/tcp') "
            "o la de firewall-cmd (p. ej. 'firewall-cmd --add-port=80/tcp'), o usa 'Añadir regla'.",
        "Invalid Destination": "Destino no válido",
        _BAD_ADDRESS: "'{value}' no es una dirección IP o red válida (p. ej. 192.168.0.0/24).",
        "Invalid Port": "Puerto no válido",
        _BAD_PORT:
            "'{value}' no es un puerto, rango de puertos o nombre de servicio válido "
            "(p. ej. 22, 6000:6007, 80,443 o ssh).",
        "Invalid Interface": "Interfaz no válida",
        "'{value}' is not a valid network interface name.":
            "'{value}' no es un nombre de interfaz de red válido.",
        "Reading the current firewall rules requires administrator rights.":
            "Leer las reglas actuales del cortafuegos requiere derechos de administrador.",
        "No existing firewall rules were found.": "No se encontraron reglas de cortafuegos existentes.",
        "{n} rule(s) found. Replace the current list or merge them into it?":
            "Se encontraron {n} regla(s). ¿Reemplazar la lista actual o combinarlas con ella?",
        "Replace": "Reemplazar",
        "Merge": "Combinar",
        _SKIPPED:
            "No se pudieron importar {n} regla(s) porque usan funciones que este editor no admite "
            "(p. ej. perfiles de aplicación o enrutamiento).",
        "'{tool}' is not installed or not available.": "'{tool}' no está instalado o no está disponible.",
        "The firewall query failed: {detail}": "La consulta al cortafuegos falló: {detail}",
        "unknown error": "error desconocido",
        "Unsupported firewall backend: {backend}": "Motor de cortafuegos no admitido: {backend}",
    },
}

for _lang, _entries in _NEW.items():
    LANGUAGES.setdefault(_lang, {}).update(_entries)
