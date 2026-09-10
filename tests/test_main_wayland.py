"""Choix de la plateforme Qt sous Wayland (voir sentinelle.__main__)."""

import sys

import pytest

from sentinelle.__main__ import _forcer_xcb_si_wayland


@pytest.fixture
def linux_wayland(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)


def test_variable_absente_force_xcb(linux_wayland, monkeypatch):
    _forcer_xcb_si_wayland()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == "xcb;wayland"


def test_wayland_herite_de_la_session_remplace(linux_wayland, monkeypatch):
    # Budgie 10.10 (startbudgielabwc) exporte QT_QPA_PLATFORM=wayland partout.
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    _forcer_xcb_si_wayland()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == "xcb;wayland"


@pytest.mark.parametrize("valeur", ["xcb", "offscreen", "eglfs", "minimal"])
def test_autre_valeur_explicite_respectee(linux_wayland, monkeypatch, valeur):
    monkeypatch.setenv("QT_QPA_PLATFORM", valeur)
    _forcer_xcb_si_wayland()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == valeur


def test_hors_wayland_sans_effet(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    _forcer_xcb_si_wayland()
    import os
    assert os.environ["QT_QPA_PLATFORM"] == "wayland"
