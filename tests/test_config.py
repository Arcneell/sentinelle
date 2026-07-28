"""Tests du modèle de configuration : obfuscation, aller-retour disque,
validation des entrées et purge des séquences. Aucun Qt requis."""

from sentinelle.config import (AppConfig, Camera, Etape, Sequence, Site,
                               desobfusquer, load_config, obfusquer,
                               purger_cameras_sequences, save_config)


def test_obfuscation_aller_retour():
    for clair in ("", "abc", "pä$$w0rd!", "é" * 10, "spaces and 日本語"):
        assert desobfusquer(obfusquer(clair)) == clair
    assert obfusquer("") == ""
    assert obfusquer("x").startswith("obf:")
    # une valeur non préfixée est rendue telle quelle (compat ancien format clair)
    assert desobfusquer("en-clair") == "en-clair"


def test_save_load_aller_retour(tmp_path):
    chemin = str(tmp_path / "config.yaml")
    cfg = AppConfig(path=chemin)
    site = Site(id="s1", nom="Site", lien="4g")
    cfg.sites.append(site)
    cfg.cameras.append(Camera(id="c1", nom="Cam", site=site, marque="hikvision",
                              hote="10.0.0.9", canal=3, user="u", password="secret-xyz"))
    cfg.sequences.append(Sequence(nom="Ronde", etapes=[
        Etape(mode="mono", cameras=["c1"], duree_s=5)]))
    save_config(cfg)

    out = load_config(chemin)
    assert not out.warnings
    assert [c.id for c in out.cameras] == ["c1"]
    assert out.camera("c1").password == "secret-xyz"     # déchiffré au chargement
    assert out.site("s1").lien == "4g"
    assert out.sequences[0].etapes[0].cameras == ["c1"]

    # le mot de passe n'apparaît jamais en clair dans le fichier
    brut = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "secret-xyz" not in brut
    assert "obf:" in brut


def test_entrees_invalides_signalees(tmp_path):
    chemin = tmp_path / "config.yaml"
    chemin.write_text(
        "sites:\n"
        "  - {id: s1, nom: S, lien: fibre}\n"
        "cameras:\n"
        "  - {id: c1, nom: A, site: s1, marque: hikvision, hote: h}\n"
        "  - {id: c1, nom: doublon, site: s1, marque: hikvision, hote: h}\n"
        "  - {id: c2, nom: B, site: inexistant, marque: hikvision, hote: h}\n"
        "  - {id: c3, nom: C, site: s1, marque: marqueinconnue, hote: h}\n"
        "  - {id: c4, nom: D, site: s1, marque: hikvision}\n",
        encoding="utf-8")
    cfg = load_config(str(chemin))
    # seule la première caméra valide subsiste ; les 4 autres sont signalées
    assert [c.id for c in cfg.cameras] == ["c1"]
    assert len(cfg.warnings) >= 4


def test_config_illisible_ne_bloque_pas(tmp_path):
    chemin = tmp_path / "config.yaml"
    chemin.write_text(": : : pas du yaml valide : [", encoding="utf-8")
    cfg = load_config(str(chemin))
    assert cfg.cameras == []
    assert cfg.warnings                                   # signalé, pas de crash


def test_purge_sequences():
    cfg = AppConfig()
    cfg.sites.append(Site(id="s1", nom="S"))
    cfg.sequences.append(Sequence(nom="R", etapes=[
        Etape(mode="grille", cameras=["c1", "c2"], duree_s=5),
        Etape(mode="mono", cameras=["c2"], duree_s=5),
    ]))
    purger_cameras_sequences(cfg, {"c2"})
    # c2 retirée de l'étape grille ; l'étape mono (uniquement c2) disparaît
    assert cfg.sequences[0].etapes[0].cameras == ["c1"]
    assert len(cfg.sequences[0].etapes) == 1


def test_url_rtsp_identifiants_encodes():
    site = Site(id="s1", nom="S")
    cam = Camera(id="c1", nom="C", site=site, marque="hikvision",
                 hote="192.0.2.10", port=554, canal=2,
                 user="admin", password="p@ss:word")
    url = cam.url("main")
    # identifiants URL-encodés (@ et : ne doivent pas casser le netloc)
    assert "p%40ss%3Aword" in url
    assert url.startswith("rtsp://admin:")
    assert "192.0.2.10:554" in url
