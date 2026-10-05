"""网络相关工具函数"""

from starlette.requests import Request

def get_client_ip(request: Request) -> str:
    """获取客户端真实 IP
    
    考虑反向代理（X-Forwarded-For, X-Real-IP）的情况。
    """
    # 优先从 X-Forwarded-For 获取
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For 可能包含多个 IP，取第一个
        return forwarded.split(",")[0].strip()
    
    # 尝试从 X-Real-IP 获取
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # 回退到直接连接 IP
    if request.client:
        return request.client.host
        
    return "unknown"
