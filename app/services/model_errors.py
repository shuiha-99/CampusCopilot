class ModelGatewayError(Exception):
    """所有模型调用异常的共同父类。"""


class ModelTimeoutError(ModelGatewayError):
    """模型在规定时间内没有返回结果。"""


class ModelConnectionError(ModelGatewayError):
    """应用无法连接模型服务。"""


class ModelAuthenticationError(ModelGatewayError):
    """模型服务的密钥无效或没有权限。"""


class ModelResponseError(ModelGatewayError):
    """模型返回了缺失或无法使用的内容。"""

