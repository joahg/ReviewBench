"""Installable ReviewBench run planning and strict result validation."""

from pathlib import Path
import runpy


def check_job_main():
    runpy.run_path(str(Path(__file__).with_name("check-job.py")))["main"]()


def check_dataset_main():
    runpy.run_path(str(Path(__file__).with_name("check-format.py")))["main"]()
