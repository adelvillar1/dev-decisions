"""devdec — dev-decisions internals (split from the dev_decisions.py monolith)."""

from . import config, judgment, gitops, workflow, gates, corpora, dashboard, cli

for _n, _v in vars(config).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(judgment).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(gitops).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(workflow).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(gates).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(corpora).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(dashboard).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
for _n, _v in vars(cli).items():
    if not _n.startswith('__'):
        globals().setdefault(_n, _v)
