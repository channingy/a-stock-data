"""共享工具：日志配置"""

import logging
import os
from pathlib import Path


def setup_logger(name="etf_collector", log_dir="./logs", level=logging.INFO):
    """配置日志
    
    Args:
        name: 日志器名称
        log_dir: 日志目录
        level: 日志级别
    
    Returns:
        logging.Logger
    """
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 文件 handler
    log_file = os.path.join(log_dir, f"{name}.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    
    # 控制台 handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    # 格式
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    return logger


def get_log_path(log_dir="./logs"):
    """获取日志目录路径"""
    return str(Path(log_dir).resolve())
