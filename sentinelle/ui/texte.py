"""Petits utilitaires de texte pour l'interface."""


def compte(n: int, singulier: str, pluriel: str | None = None) -> str:
    """Accord correct : « 1 étape », « 3 étapes », « 0 caméra ».

    Chaque mot du libellé prend le pluriel fourni tel quel :
    compte(2, "canal trouvé", "canaux trouvés") → « 2 canaux trouvés »."""
    if pluriel is None:
        pluriel = singulier + "s"
    return f"{n} {pluriel if n > 1 else singulier}"
