"""Watch Doubao's chat page and log what actually changes while it works.

Diagnostic only: one ``Runtime.evaluate`` per tick, no clicks, no writes to the
page. It exists because the 工作 (work) composer exposes no "generating" text at
all — the stop control is always visible and the send button carries a
permanent ``!hidden`` class — so the live state has to be inferred from what
moves on screen, not from a label.

Usage:
    python tools/doubao_state_watch.py --seconds 300 --out build/doubao_watch.log
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doubao_reader as d  # noqa: E402

FINGERPRINT_JS = r"""
(() => {
  const g = (k) => document.querySelector(`[data-testid="${k}"]`);
  const info = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [Math.round(r.width), Math.round(r.height), !!el.offsetParent];
  };
  const msgs = [...document.querySelectorAll('[data-testid="receive_message"]')];
  const sends = [...document.querySelectorAll('[data-testid="send_message"]')];
  // The last counter belongs to the newest reply; the earlier ones are frozen
  // at whatever their reply took and would hide the live one.
  const timers = [...document.querySelectorAll('[data-testid="preamble-elapsed-timer"]')]
    .map((n) => (n.innerText || '').trim());
  const lastTimer = timers.length ? timers[timers.length - 1] : '';
  const ids = [...new Set([...document.querySelectorAll('[data-testid]')]
    .map((e) => e.getAttribute('data-testid')))].sort();
  const body = document.body ? document.body.innerText || '' : '';
  return JSON.stringify({
    href: location.href,
    title: document.title,
    bodyLen: body.length,
    msgCount: msgs.length,
    lastMsgLen: msgs.length ? (msgs[msgs.length - 1].innerText || '').length : 0,
    userCount: sends.length,
    timer: (g('preamble-elapsed-timer') || {}).innerText || '',
    lastTimer: lastTimer,
    preamble: (g('preamble-elapsed') || {}).innerText || '',
    breakBtn: info(g('chat_input_local_break_button')),
    skeleton: info(g('office-loading-skeleton-docx')),
    ids: ids,
    tail: body.slice(-120),
  });
})()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=d.DEFAULT_CDP_PORT)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--out", default="build/doubao_watch.log")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    handle = out_path.open("w", encoding="utf-8", buffering=1)
    started = time.time()
    previous: dict | None = None
    baseline_ids: set[str] = set()
    target_url = ""
    ticks = 0

    def emit(line: str) -> None:
        handle.write(line + "\n")

    emit(f"# watch start {time.strftime('%H:%M:%S')} port={args.port} seconds={args.seconds}")

    while time.time() - started < args.seconds:
        ticks += 1
        try:
            targets = d.chat_targets(d.cdp_targets(args.port))
        except Exception as exc:  # noqa: BLE001
            emit(f"{time.strftime('%H:%M:%S')} PORT-ERR {type(exc).__name__}: {str(exc)[:90]}")
            time.sleep(max(args.interval, 1.0))
            continue
        if not targets:
            emit(f"{time.strftime('%H:%M:%S')} NO-CHAT-PAGE")
            time.sleep(max(args.interval, 1.0))
            continue
        target = targets[0]
        url = str(target.get("url") or "")
        if url != target_url:
            target_url = url
            previous = None
            baseline_ids = set()
            emit(f"{time.strftime('%H:%M:%S')} PAGE {url}")
        try:
            raw = d.evaluate(str(target["webSocketDebuggerUrl"]), FINGERPRINT_JS, timeout=3.0)
            info = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            emit(f"{time.strftime('%H:%M:%S')} EVAL-ERR {type(exc).__name__}: {str(exc)[:90]}")
            time.sleep(max(args.interval, 1.0))
            continue

        ids = set(info.get("ids") or [])
        if not baseline_ids:
            baseline_ids = ids
            emit(f"{time.strftime('%H:%M:%S')} BASELINE ids={len(ids)}")
        new_ids = sorted(ids - baseline_ids)
        gone_ids = sorted(baseline_ids - ids)

        stamp = time.strftime("%H:%M:%S")
        summary = (
            f"body={info.get('bodyLen')} msg={info.get('msgCount')}/{info.get('lastMsgLen')} "
            f"user={info.get('userCount')} timer={info.get('timer')!r} "
            f"break={info.get('breakBtn')} skel={info.get('skeleton')}"
        )
        if previous is None:
            emit(f"{stamp} FIRST {summary}")
        else:
            changed = [k for k in ("bodyLen", "msgCount", "lastMsgLen", "userCount", "timer", "lastTimer", "preamble")
                       if info.get(k) != previous.get(k)]
            if changed or new_ids or gone_ids:
                detail = " ".join(f"{k}:{previous.get(k)}->{info.get(k)}" for k in changed)
                emit(f"{stamp} CHANGE {detail} +ids={new_ids[:6]} -ids={gone_ids[:6]}")
                emit(f"        {summary}")
                if new_ids:
                    emit(f"        tail={info.get('tail')!r}")
        previous = info
        time.sleep(args.interval)

    emit(f"# watch end {time.strftime('%H:%M:%S')} ticks={ticks}")
    handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
