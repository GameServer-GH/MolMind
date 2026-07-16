"""统一候选资格门控。"""

from services.eligibility.policy import evaluate_candidate_eligibility, policy_from_config

__all__ = ["evaluate_candidate_eligibility", "policy_from_config"]
