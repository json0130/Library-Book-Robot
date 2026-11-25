"""Scenario tests: scripted user scenarios run against the real robot core, many times, with a report.

    python -m robot scenarios list
    python -m robot scenarios run --scenario all --runs 10 --llm mock --speed 10
    python -m robot scenarios report data/eval_runs/<timestamp>
    python -m robot scenarios replay <run id>
    python -m robot scenarios compare DIR_A DIR_B
    python -m robot scenarios live --scenario return_book        # a human tester on the real robot

See docs/testing.md. This package only observes the robot: it never changes how it behaves.
"""
