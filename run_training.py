"""
run_training.py

CLI entry point to train all FlowSync ML models from scratch.
Usage:
    python run_training.py                  # train demand + expiry
    python run_training.py --model demand   # demand only
    python run_training.py --model expiry   # expiry only
    python run_training.py --stores 10      # use 10 stores (faster)
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("flowsync.training")


def main():
    parser = argparse.ArgumentParser(description="FlowSync ML Training Pipeline")
    parser.add_argument(
        "--model",
        choices=["demand", "expiry", "all"],
        default="all",
        help="Which model to train (default: all)",
    )
    parser.add_argument(
        "--stores",
        type=int,
        default=20,
        help="Number of Rossmann stores to use for demand training (default: 20)",
    )
    parser.add_argument(
        "--mape-target",
        type=float,
        default=0.15,
        help="MAPE target for demand model (default: 0.15)",
    )
    parser.add_argument(
        "--auc-target",
        type=float,
        default=0.80,
        help="AUC target for expiry model (default: 0.80)",
    )
    args = parser.parse_args()

    from ml.pipeline.auto_trainer import AutoTrainer

    trainer = AutoTrainer()
    results = {}

    if args.model in ("demand", "all"):
        logger.info("Starting demand forecaster training...")
        results["demand"] = trainer.train_demand(
            n_stores=args.stores,
            mape_target=args.mape_target,
        )
        status = results["demand"]["status"]
        if status in ("success", "saved_below_target"):
            logger.info(
                f"Demand model SAVED  MAPE: {results['demand']['mape_cv']:.2%}"
                + (" [below MAPE target]" if status == "saved_below_target" else "")
            )
        else:
            logger.warning(
                f"Demand model NOT saved  MAPE: {results['demand'].get('mape_cv', '?')}"
            )

    if args.model in ("expiry", "all"):
        logger.info("Starting expiry risk model training...")
        results["expiry"] = trainer.train_expiry(
            auc_target=args.auc_target,
        )
        status = results["expiry"]["status"]
        if status == "success":
            logger.info(
                f"Expiry model SAVED  —  "
                f"AUC: {results['expiry']['auc_cv']:.3f}  |  "
                f"Threshold: {results['expiry']['optimal_threshold']:.3f}"
            )
        else:
            logger.warning(
                f"Expiry model NOT saved  —  "
                f"AUC: {results['expiry'].get('auc_cv', '?')}"
            )

    # Summary
    print("\n" + "=" * 60)
    print("TRAINING SUMMARY")
    print("=" * 60)
    for model, res in results.items():
        icon = "OK" if res["status"] in ("success", "saved_below_target") else "FAIL"
        print(f"  [{icon}] {model:10s}  status={res['status']}", end="")
        if model == "demand" and "mape_cv" in res:
            print(f"  MAPE={res['mape_cv']:.2%}", end="")
        if model == "expiry" and "auc_cv" in res:
            print(
                f"  AUC={res['auc_cv']:.3f}  "
                f"threshold={res.get('optimal_threshold', '?')}",
                end="",
            )
        print()
    print("=" * 60)


if __name__ == "__main__":
    main()
