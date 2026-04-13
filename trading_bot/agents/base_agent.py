from abc import ABC, abstractmethod

class BaseAgent(ABC):
    """
    시장을 분석하고 의사결정을 내리는 에이전트의 기본 인터페이스.
    LLM 또는 Rule-base 모델이 이를 상속받아 판단(Decision Making) 과정을 구현합니다.
    """
    
    def __init__(self):
        self.skills = {}

    def register_skill(self, name: str, skill):
        """에이전트가 사용할 수 있는 스킬을 등록합니다."""
        self.skills[name] = skill

    @abstractmethod
    def analyze_and_act(self, event: dict, context: dict):
        """
        이벤트(틱, 봉, 조건식 등)와 맥락(잔고, 시장 상황 등)을 받아
        분석 후 적절한 스킬을 호출하여 행동(Action)을 취합니다.
        
        :param event: 발생한 실시간 이벤트 (예: {"type": "TICK", "code": "005930", "price": 80000})
        :param context: 에이전트가 참조할 수 있는 현재 파이프라인의 상태 정보
        """
        pass
