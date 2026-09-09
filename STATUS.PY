
class STATUS:
    """状态类，记录当前任务状态"""
    def __init__(self, status: str = "init"):
        self.status = status
        if status not in ["init", "planning","running","completed"]:
            raise ValueError(f"无效状态: {status}")
