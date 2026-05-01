"""Authoring macOS Shortcuts.

There is no `shortcuts create`, but `shortcuts sign` accepts an *input* file —
so a shortcut can be authored as a plist, signed, and handed to the user. Opening
the signed file makes Shortcuts show its Add sheet with every action listed, so
the user reviews and approves before anything lands in their library. That
confirmation is the reason this is safe to expose at all.

The constraint is the action vocabulary. Shortcuts has hundreds of actions with
undocumented identifiers and parameter shapes, and a 4-bit 4B model asked to
invent them produces plausible nonsense that fails silently. So the model does
not write actions — it picks from `VOCABULARY` below, and anything outside it is
rejected before a file is written. Small and correct beats broad and broken.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from typing import Any

SHORTCUTS_BIN = "/usr/bin/shortcuts"

def _bool(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


# Bundle ids for apps people actually name out loud. Anything else falls back to
# a plain name, which Shortcuts resolves when the shortcut is added.
_BUNDLES = {
    "safari": "com.apple.Safari", "music": "com.apple.Music",
    "mail": "com.apple.mail", "messages": "com.apple.MobileSMS",
    "notes": "com.apple.Notes", "calendar": "com.apple.iCal",
    "reminders": "com.apple.reminders", "photos": "com.apple.Photos",
    "spotify": "com.spotify.client", "finder": "com.apple.finder",
    "terminal": "com.apple.Terminal", "maps": "com.apple.Maps",
    "system settings": "com.apple.systempreferences",
}


def _app(v: Any) -> dict:
    name = str(v).strip()
    bundle = _BUNDLES.get(name.lower(), f"com.apple.{name.replace(' ', '')}")
    return {"WFSelectedApp": {"BundleIdentifier": bundle, "Name": name}}


# step type -> (action identifier, builder taking the step's value)
#
# Deliberately curated. Everything here does something real; the earlier set was
# all notifications and waits, which built a valid shortcut that accomplished
# nothing. Identifiers are Shortcuts' own and undocumented, so this list only
# grows with ones whose parameter shape is known.
VOCABULARY: dict[str, tuple[str, Any]] = {
    # doing things
    "open_app": ("is.workflow.actions.openapp", _app),
    "quit_app": ("is.workflow.actions.quitapp", _app),
    "run_shortcut": ("is.workflow.actions.runworkflow",
                     lambda v: {"WFWorkflowName": str(v)}),
    "open_url": ("is.workflow.actions.openurl",
                 lambda v: {"WFInput": str(v)}),
    # media
    "music": ("is.workflow.actions.pausemusic",
              lambda v: {"WFPlayPauseBehavior":
                         {"play": "Play", "pause": "Pause"}.get(
                             str(v).strip().lower(), "Play/Pause")}),
    "next_track": ("is.workflow.actions.skipforward", lambda v: {}),
    "previous_track": ("is.workflow.actions.skipback", lambda v: {}),
    # system state
    "set_focus": ("is.workflow.actions.dnd.set",
                  lambda v: {"Enabled": _bool(v)}),
    "set_wifi": ("is.workflow.actions.wifi.set",
                 lambda v: {"OnValue": _bool(v)}),
    "set_bluetooth": ("is.workflow.actions.bluetooth.set",
                      lambda v: {"OnValue": _bool(v)}),
    "set_low_power": ("is.workflow.actions.lowpowermode.set",
                      lambda v: {"On": _bool(v)}),
    "set_volume": ("is.workflow.actions.setvolume",
                   lambda v: {"WFVolume": max(0.0, min(1.0, float(v)))}),
    "set_brightness": ("is.workflow.actions.setbrightness",
                       lambda v: {"WFBrightness": max(0.0, min(1.0, float(v)))}),
    # output / control flow
    "notify": ("is.workflow.actions.notification",
               lambda v: {"WFNotificationActionBody": str(v),
                          "WFNotificationActionSound": True}),
    "say": ("is.workflow.actions.speaktext",
            lambda v: {"WFText": str(v)}),
    "show": ("is.workflow.actions.showresult",
             lambda v: {"Text": str(v)}),
    "wait": ("is.workflow.actions.delay",
             lambda v: {"WFDelayTime": float(v)}),
    "comment": ("is.workflow.actions.comment",
                lambda v: {"WFCommentActionText": str(v)}),
}


class ShortcutError(Exception):
    pass


def build(name: str, steps: list[dict]) -> dict:
    """Steps → an unsigned shortcut plist dict. Raises on anything unsupported
    rather than emitting an action that would fail quietly on the user's Mac."""
    if not steps:
        raise ShortcutError("a shortcut needs at least one step")
    actions = []
    for i, step in enumerate(steps, 1):
        kind = str(step.get("type", "")).strip().lower()
        if kind not in VOCABULARY:
            raise ShortcutError(
                f"step {i} uses '{kind or '?'}', which I can't build. "
                f"Supported: {', '.join(sorted(VOCABULARY))}")
        identifier, make_params = VOCABULARY[kind]
        try:
            params = make_params(step.get("value", ""))
        except (TypeError, ValueError):
            raise ShortcutError(f"step {i} ({kind}) has an unusable value")
        actions.append({"WFWorkflowActionIdentifier": identifier,
                        "WFWorkflowActionParameters": params})
    return {
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 2071128575,
                           "WFWorkflowIconGlyphNumber": 61440},
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowActions": actions,
    }


def write_signed(name: str, steps: list[dict]) -> str:
    """Author, sign, and return a path to the .shortcut file for the app to open.

    Signing is Apple's own tool and needs the user's login session — which the
    backend has, running as them. `--mode anyone` avoids the signing service
    refusing a file that was never shared.
    """
    if not os.path.exists(SHORTCUTS_BIN):
        raise ShortcutError("the shortcuts tool isn't available on this Mac")
    plist = build(name, steps)
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "Shortcut"
    tmp = tempfile.mkdtemp(prefix="myai-shortcut-")
    # Both ends need the .shortcut extension — the signing tool rejects any
    # other input filename with "isn't in the correct format", which reads like
    # a plist problem and isn't.
    os.makedirs(os.path.join(tmp, "unsigned"), exist_ok=True)
    unsigned = os.path.join(tmp, "unsigned", f"{safe}.shortcut")
    signed = os.path.join(tmp, f"{safe}.shortcut")
    with open(unsigned, "wb") as fh:
        plistlib.dump(plist, fh, fmt=plistlib.FMT_BINARY)
    proc = subprocess.run(
        [SHORTCUTS_BIN, "sign", "--mode", "anyone",
         "--input", unsigned, "--output", signed],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0 or not os.path.exists(signed):
        # stderr is full of harmless ObjC runtime noise; surface the tail only.
        detail = (proc.stderr or "").strip().splitlines()[-1:] or ["signing failed"]
        raise ShortcutError(detail[0][:160])
    return signed
