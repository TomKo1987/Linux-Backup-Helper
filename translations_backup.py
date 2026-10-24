from translations import LANGUAGES

_NEW: dict[str, dict[str, str]] = {
    "Deutsch": {
        "Permission denied — file not readable": "Zugriff verweigert – Datei nicht lesbar",
        "Source file vanished during backup": "Quelldatei während der Sicherung verschwunden",
        "Destination drive '{drive}' is not mounted — entry skipped": "Ziellaufwerk '{drive}' ist nicht eingehängt – Eintrag übersprungen",
        "Pruned old version": "Alte Version entfernt",
        "Version incomplete — kept for review": "Version unvollständig – zur Prüfung behalten",
        "Restoring from the newest complete version: {name}": "Wiederherstellung aus der neuesten vollständigen Version: {name}",
        "Restore": "Wiederherstellen",
        "Writing data to disk…": "Daten werden auf den Datenträger geschrieben …",
        "Restore selected entries? Existing files with the same name will be overwritten.": "Ausgewählte Einträge wiederherstellen? Vorhandene Dateien mit gleichem Namen werden überschrieben.",
        "Pre-hook failed — entry skipped": "Pre-Hook fehlgeschlagen – Eintrag übersprungen",
        "Could not flush data to the destination device: {err}": "Daten konnten nicht auf das Zielgerät geschrieben werden: {err}",
    },
    "Français": {
        "Permission denied — file not readable": "Permission refusée — fichier illisible",
        "Source file vanished during backup": "Fichier source disparu pendant la sauvegarde",
        "Destination drive '{drive}' is not mounted — entry skipped": "Le lecteur de destination « {drive} » n'est pas monté — entrée ignorée",
        "Pruned old version": "Ancienne version supprimée",
        "Version incomplete — kept for review": "Version incomplète — conservée pour vérification",
        "Restoring from the newest complete version: {name}": "Restauration depuis la dernière version complète : {name}",
        "Restore": "Restaurer",
        "Writing data to disk…": "Écriture des données sur le disque…",
        "Restore selected entries? Existing files with the same name will be overwritten.": "Restaurer les entrées sélectionnées ? Les fichiers existants du même nom seront écrasés.",
        "Pre-hook failed — entry skipped": "Échec du pre-hook — entrée ignorée",
        "Could not flush data to the destination device: {err}": "Impossible d'écrire les données sur le périphérique de destination : {err}",
    },
    "Español": {
        "Permission denied — file not readable": "Permiso denegado — archivo no legible",
        "Source file vanished during backup": "Archivo de origen desaparecido durante la copia",
        "Destination drive '{drive}' is not mounted — entry skipped": "La unidad de destino '{drive}' no está montada — entrada omitida",
        "Pruned old version": "Versión antigua eliminada",
        "Version incomplete — kept for review": "Versión incompleta — conservada para revisión",
        "Restoring from the newest complete version: {name}": "Restaurando desde la versión completa más reciente: {name}",
        "Restore": "Restaurar",
        "Writing data to disk…": "Escribiendo datos en el disco…",
        "Restore selected entries? Existing files with the same name will be overwritten.": "¿Restaurar las entradas seleccionadas? Los archivos existentes con el mismo nombre se sobrescribirán.",
        "Pre-hook failed — entry skipped": "Pre-hook fallido — entrada omitida",
        "Could not flush data to the destination device: {err}": "No se pudieron escribir los datos en el dispositivo de destino: {err}",
    },
}


def register_backup_translations() -> None:
    for lang, entries in _NEW.items():
        table = LANGUAGES.get(lang)
        if table is not None:
            for key, value in entries.items():
                table.setdefault(key, value)
