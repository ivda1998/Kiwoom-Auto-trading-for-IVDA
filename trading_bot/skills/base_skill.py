from abc import ABC, abstractmethod

class BaseSkill(ABC):
    """
    에이전트가 사용할 수 있는 스킬(행동/명령)의 기본 인터페이스.
    각 스킬은 특정 행동(예: 시장 데이터 조회, 주문 실행 등)을 캡슐화합니다.
    """
    
    @abstractmethod
    def __init__(self, kiwoom_api):
        """
        :param kiwoom_api: 통신을 담당할 키움 API 인스턴스 (또는 데이터 소스)
        """
        self.api = kiwoom_api
        
    @abstractmethod
    def execute(self, *args, **kwargs):
        """
        해당 스킬을 실행합니다.
        에이전트는 이 메서드를 통해 스킬을 구동시킵니다.
        """
        pass
