/* Market simulator (ADR-106 ... ADR-110): NQ and ES on the DISCOVERY period, analysed deeply, and live-knowledge
   15-minute forecasts scored on months, holdout days and new days the models never saw. Nothing here is a backtest, a
   try or a strategy. One file per tab in ./market/ (ADR-110: seven tabs by purpose). */
export { MarketStartPage } from "./market/start";
export { MarketBehaviourPage } from "./market/behaviour";
export { MarketPatternsPage } from "./market/patterns";
export { MarketPredictionsPage } from "./market/predictions";
export { MarketDayReplayPage } from "./market/replay";
export { MarketLivePage } from "./market/live";
export { MarketHoldoutPage } from "./market/holdout";
