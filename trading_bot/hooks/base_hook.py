from abc import ABC, abstractmethod

class BaseHook(ABC):
    """
    에이전트의 작업 흐름(Lifecycle)에 개입하는 훅(Hook)의 기본 인터페이스.
    파이프라인의 특정 시점(예: 매수 직전, 틱 수신 시점 등)에 실행됩니다.
    """
    
    @abstractmethod
    def pre_action(self, context: dict, action: dict) -> bool:
        """
        에이전트가 행동을 취하기 전에 호출됩니다.
        :param context: 현재 시장 상태 및 봇 상태 데이터
        :param action: 에이전트가 취하려는 행동 데이터
        :return: 행동 수행 허용 여부 (False 시 행동 차단)
        """
        return True

    @abstractmethod
    def post_action(self, context: dict, action: dict, result) -> None:
        """
        에이전트가 행동을 취한 직후에 호출됩니다. 로깅, 상태 업데이트 등에 사용됩니다.
        """
        pass
