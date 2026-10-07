"""레버리지 에이전트 포지션 디스크 영속화 — 재시작 시 원 에이전트가 재adopt하기 위함.

배경(2026-10-07 실사고): 레버리지 두 에이전트는 포지션이 인메모리라 보유 중 재시작하면
실계좌 주식이 완전 고아(SL/트레일/EOD 전부 무작동)가 됐다. vm_manager처럼 디스크에
영속시키고, 재시작 시 실계좌(get_positions)와 대조해 재adopt한다.
관련: feedback_cross_agent_position_ownership 메모리.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def save_position(path: str, position: dict | None) -> None:
    """position이 None이면 파일 삭제(= 보유 없음), 아니면 JSON으로 저장."""
    try:
        if position is None:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(position, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[position_store] 저장 실패 {path}: {e}")


def load_position(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"[position_store] 로드 실패 {path}: {e}")
        return None
