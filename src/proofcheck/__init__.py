"""proofcheck: evidence, not authorship.

Check whether a pull request actually does what it claims -- by running its
tests on the base and head commits, reproducing referenced issues, and
matching the PR's claims to the evidence -- regardless of who or what wrote it.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
