"""Paper trading (ADR-81): selected strategies trade NEW, unseen Dukascopy data forward, each in a simulated prop account.

Kept separate from research (CLAUDE.md principle 12): the forward feed is never stored as a research dataset, paper
accounts are never research runs or protocol trials, and no order is ever sent anywhere. Modules: ``feed`` (daily
download + validation), ``engine`` (forward trades + prop attempts), ``store`` (account records), ``manager``
(background updates)."""
