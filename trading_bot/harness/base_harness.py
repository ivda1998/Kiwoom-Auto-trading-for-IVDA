from abc import ABC, abstractmethod
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class BaseHarness(ABC):
    """
    통신 및 런타임 이벤트 버스 역할을 수행하는 하네스 기저 클래스.
    이벤트를 등록된 에이전트(Agent)들에 전달하고,
    에이전트가 행동(Action)을 취할 때 등록된 훅(Hook) 파이프라인을 구동합니다.
    """
    def __init__(self):
        self.agents = []
        self.hooks = []
        self.skills = {}
        self.context = {}

    def register_agent(self, agent):
        self.agents.append(agent)
        logger.info(f"[Harness] Registered Agent: {agent.__class__.__name__}")

    def register_hook(self, hook):
        self.hooks.append(hook)
        logger.info(f"[Harness] Registered Hook: {hook.__class__.__name__}")

    def register_skill(self, name: str, skill):
        self.skills[name] = skill
        logger.info(f"[Harness] Registered Skill: {name} -> {skill.__class__.__name__}")

    def get_context(self) -> dict:
        return self.context

    def update_context(self, key: str, value: Any):
        self.context[key] = value

    def broadcast_event(self, event_type: str, event_data: dict):
        """실시간 이벤트를 에이전트들에게 브로드캐스트합니다."""
        event = {"type": event_type, "data": event_data}
        context = self.get_context()
        for agent in self.agents:
            try:
                agent.analyze_and_act(event, context)
            except Exception as e:
                logger.error(f"[Harness] 에이전트 {agent.__class__.__name__} 이벤트 처리 에러: {e}", exc_info=True)

    def execute_action(self, agent, skill_name: str, action_type: str, *args, **kwargs) -> Any:
        """
        에이전트가 행동을 개시할 때 스킬을 대행 구동하고 훅 파이프라인을 인가합니다.
        
        :param agent: 행동을 촉발한 에이전트
        :param skill_name: 구동할 스킬 식별자 (예: 'execution')
        :param action_type: 스킬 내부의 특정 행동 유형 (예: 'BUY', 'SELL')
        """
        context = self.get_context()
        action = {
            "agent": agent,
            "skill": skill_name,
            "type": action_type,
            "args": args,
            "kwargs": kwargs
        }

        # 1. Pre-trade Hooks 실행
        for hook in self.hooks:
            try:
                if not hook.pre_action(context, action):
                    logger.warning(f"[Harness] 훅 {hook.__class__.__name__}에 의해 행동 차단됨: {action_type} - args={args}")
                    return False
            except Exception as e:
                logger.error(f"[Harness] 훅 {hook.__class__.__name__} pre_action 실행 에러: {e}", exc_info=True)
                return False

        # 2. Skill 실행
        skill = self.skills.get(skill_name)
        if not skill:
            logger.error(f"[Harness] 스킬이 등록되지 않았습니다: {skill_name}")
            return False

        try:
            result = skill.execute(action_type, *args, **kwargs)
        except Exception as e:
            logger.error(f"[Harness] 스킬 {skill_name} 실행 에러: {e}", exc_info=True)
            result = False

        # 3. Post-trade Hooks 실행
        for hook in self.hooks:
            try:
                hook.post_action(context, action, result)
            except Exception as e:
                logger.error(f"[Harness] 훅 {hook.__class__.__name__} post_action 실행 에러: {e}", exc_info=True)

        return result
