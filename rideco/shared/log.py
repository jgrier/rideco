"""Per-service activity logging.

Each service writes to its own stdout (segregated per-service in the
TUI), so log lines don't need to repeat which service they're from.
Three helpers, one shape per direction of activity:

  log_in(handler, **kv)        — incoming handler invocation
  log_out(kind, target, **kv)  — outgoing interaction
  log(msg, **kv)               — body messages: decisions, transitions

`kind` on log_out is a free string surfacing the call shape inline so
readers can see at a glance whether we blocked the caller or fired
durably onto the Restate log. Convention used across the services:

  "call"             — ctx.object_call / ctx.service_call (sync; caller awaits)
  "send"             — ctx.object_send / ctx.service_send (async; durable on the log)
  "send+delay(Ns)"   — ctx.object_send(..., send_delay=N) (delayed self/other send)
  "resolve"          — ctx.resolve_awakeable (resumes a suspended invocation)

Shared/polled read handlers (Features.get, *.get, Features.event_rate)
deliberately do not call log_in — they're hit every poll by the TUI
and on every Pricing refresh, so logging entry on those would drown
out everything else.
"""

from rich.console import Console

_console = Console()


def log_in(handler: str, **kv) -> None:
    """Log the entry of an incoming handler invocation."""
    suffix = " ".join(f"[dim]{k}=[/dim]{v}" for k, v in kv.items())
    _console.print(f"[bold cyan]← {handler}[/bold cyan]  {suffix}".rstrip())


def log_out(kind: str, target: str, **kv) -> None:
    """Log an outgoing interaction. `kind` ∈ {call, send, send+delay(Ns),
    resolve}; color follows sync (yellow) vs async (blue)."""
    color = "yellow" if kind == "call" else "blue"
    suffix = " ".join(f"[dim]{k}=[/dim]{v}" for k, v in kv.items())
    _console.print(
        f"[bold {color}]→ {kind} {target}[/bold {color}]  {suffix}".rstrip(),
    )


def log(msg: str, **kv) -> None:
    """Free-form body message: a decision, state transition, completion."""
    suffix = " ".join(f"[dim]{k}=[/dim]{v}" for k, v in kv.items())
    _console.print(f"{msg}  {suffix}".rstrip())
