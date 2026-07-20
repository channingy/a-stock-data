"""共享工具：限流器"""

import time
import random
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """东财限流器（基于 em_get 复用）"""
    
    def __init__(self, min_interval=1.0, jitter_range=(0.1, 0.5)):
        self.min_interval = min_interval
        self.jitter_range = jitter_range
        self._last_call = [0.0]
    
    def wait(self):
        """等待直到满足限流要求"""
        elapsed = time.time() - self._last_call[0]
        wait_time = self.min_interval - elapsed
        if wait_time > 0:
            jitter = random.uniform(*self.jitter_range)
            sleep_time = wait_time + jitter
            logger.debug(f"限流等待 {sleep_time:.2f}s")
            time.sleep(sleep_time)
        self._last_call[0] = time.time()


class MootdxRateLimiter:
    """mootdx 限流器（软限制）"""
    
    def __init__(self, call_interval=0.5, batch_size=100, batch_sleep=5):
        self.call_interval = call_interval
        self.batch_size = batch_size
        self.batch_sleep = batch_sleep
        self._call_count = 0
        self._batch_count = 0
    
    def wait_after_call(self):
        """每次调用后等待"""
        time.sleep(self.call_interval)
        self._call_count += 1
        self._batch_count += 1
        
        # 每 batch_size 次休息
        if self._batch_count >= self.batch_size:
            logger.info(f"已处理 {self._batch_count} 只品种，休息 {self.batch_sleep}s")
            time.sleep(self.batch_sleep)
            self._batch_count = 0
    
    def reset(self):
        """重置计数器"""
        self._batch_count = 0


class BatchLimiter:
    """通用批次限流器"""
    
    def __init__(self, batch_size=100, sleep_seconds=5):
        self.batch_size = batch_size
        self.sleep_seconds = sleep_seconds
        self._count = 0
    
    def check_and_wait(self):
        """检查是否需要休息"""
        self._count += 1
        if self._count >= self.batch_size:
            logger.info(f"批次 {self._count} 完成，休息 {self.sleep_seconds}s")
            time.sleep(self.sleep_seconds)
            self._count = 0
