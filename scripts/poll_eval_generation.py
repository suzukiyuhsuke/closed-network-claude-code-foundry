"""Poll Foundry evaluation generation jobs without starting an evaluation run."""

import argparse
import json
import time
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential

API_VERSION = "2025-11-15-preview"
TERMINAL_STATES = {"succeeded", "failed", "canceled"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--dataset-job-id", required=True)
    parser.add_argument("--evaluator-job-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    token = AzureCliCredential(tenant_id=args.tenant_id).get_token(
        "https://ai.azure.com/.default"
    ).token
    jobs = {
        "dataset": ("data_generation_jobs", args.dataset_job_id),
        "evaluator": ("evaluator_generation_jobs", args.evaluator_job_id),
    }
    started = time.monotonic()
    results = {}
    with httpx.Client(timeout=30) as client:
        while True:
            for name, (kind, job_id) in jobs.items():
                response = client.get(
                    f"{args.endpoint}/{kind}/{job_id}",
                    params={"api-version": API_VERSION},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                results[name] = response.json()
            statuses = {name: result["status"].lower() for name, result in results.items()}
            print(json.dumps(statuses), flush=True)
            if all(status in TERMINAL_STATES for status in statuses.values()):
                break
            if time.monotonic() - started >= args.timeout:
                raise TimeoutError(f"Generation did not finish within {args.timeout} seconds.")
            time.sleep(args.interval)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if any(status != "succeeded" for status in statuses.values()):
        raise RuntimeError(f"Generation ended without success: {statuses}")


if __name__ == "__main__":
    main()
