# Phase 9 broker confirmation questions

Use these questions for an IBKR support ticket after Phase 8 produces `BROKER_CONFIRMATION_REQUIRED`.
Record the ticket/case ID and the written answer in the Phase 9 evidence JSON. Do not infer an OSE-specific guarantee from generic combo documentation.

1. For Osaka Exchange (`OSE.JPN`) **securities options / Japanese single-stock options**, does TWS API accept a multi-leg `BAG` contract for option-only combinations?
2. If accepted, is that BAG routed **directly to OSE as one exchange-native strategy order**, routed via `SMART`, or decomposed/handled internally by IBKR?
3. For this exact product, can any leg execute before the other legs, or is the complete BAG execution guaranteed as a single atomic transaction?
4. Is `NonGuaranteed=1` required, optional, rejected, or ignored for OSE securities-option BAGs?
5. If JPX contract specifications say `Strategy Trades: Unavailable`, what execution mechanism does IBKR use when a paper or What-If BAG appears to be accepted?
6. Does paper-account BAG behavior for OSE securities options match the live routing mechanism, or can the paper simulator report a synthetic BAG fill that has no equivalent exchange-native live order?

A response is not sufficient if it only describes IBKR combo orders in general. It must explicitly identify OSE/Japanese securities options and the live routing/guarantee semantics.
