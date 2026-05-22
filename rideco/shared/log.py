"""Color-coded structured logging for stage demos.

The `flow` keyword arg prepends a flow tag so the audience can see, live,
which interactions are synchronous RPC vs Restate log async send vs Kafka
ingest vs delayed self-send. Used at the *call site* of every inter-service
interaction — the sender annotates the flow, the receiver just logs what
it did.

  flow="sync"   →  [sync→]    sync RPC (caller awaits)
  flow="send"   →  [send→]    one-way durable async send (durable async)
  flow="self"   →  [self→]    delayed self-send (cadence loop)
  flow="kafka"  →  [kafka→]   publish to a Kafka topic (external boundary)
  flow=None     →  no tag     receiver-side activity log
"""

from rich.console import Console

_console = Console()

_SERVICE_COLORS = {
    "Trip":        "bright_cyan",
    "Dispatch":    "bright_magenta",
    "Locations":   "bright_yellow",
    "Pricing":     "bright_green",
    "ETA":         "bright_blue",
    "Features":    "bright_white",
    "Offers":      "cyan",
    "SafetyAgent": "bright_red",
    "rider-sim":   "cyan",
    "driver-sim":  "yellow",
    "mapping":     "white",
}

_FLOW_TAGS = {
    "sync":  "[bold green][sync→][/bold green]   ",
    "send":  "[bold blue][send→][/bold blue]   ",
    "self":  "[bold blue][self→][/bold blue]   ",
    "kafka": "[bold red][kafka→][/bold red]  ",
}


def log(service: str, msg: str, *, flow: str | None = None, **kv) -> None:
    color = _SERVICE_COLORS.get(service, "white")
    tag = f"[{color}]{service:<11}[/{color}]"
    flow_tag = _FLOW_TAGS.get(flow, "             ")
    suffix = " ".join(f"[dim]{k}=[/dim]{v}" for k, v in kv.items())
    _console.print(f"{flow_tag}{tag} {msg} {suffix}".rstrip())
