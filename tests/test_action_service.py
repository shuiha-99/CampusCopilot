from types import SimpleNamespace

import pytest

from app.services.action_service import ActionValidationError, _validate_details, build_preparation_result


def test_scholarship_action_requires_bound_details() -> None:
    with pytest.raises(ActionValidationError, match="college"):
        _validate_details(
            "scholarship_preparation",
            {"grade": "大二", "scholarship_type": "国家励志奖学金"},
        )


def test_task_result_never_claims_school_submission() -> None:
    task = SimpleNamespace(
        action_type="major_transfer_preparation",
        payload_json=(
            '{"details":{"grade":"大一","target_major":"计算机科学与技术"}}'
        ),
    )
    result = build_preparation_result(task)
    assert "不代表已经向学校提交" in result["disclaimer"]
    assert len(result["items"]) == 5
