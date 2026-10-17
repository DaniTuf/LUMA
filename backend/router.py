import time
import threading
from collections import deque

class ProviderRouter:
    def __init__(self):
        # Thresholds set to 1 LESS than the actual limit to switch early
        # Gemini Free: 15 RPM -> Switch at 14
        self.gemini_limit_rpm = 14
        self.gemini_limit_rpd = 1490 # Leave buffer for RPD
        
        # Groq Free: 30 RPM -> Switch at 29
        self.groq_limit_rpm = 29
        self.groq_limit_rpd = 14300
        
        self.windows = {
            "gemini": {"minute": deque(), "day": 0, "day_start": time.time()},
            "groq": {"minute": deque(), "day": 0, "day_start": time.time()}
        }
        self.lock = threading.Lock()

    def _clean_window(self, window):
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()

    def get_provider(self) -> tuple[str | None, float]:
        """
        Returns (provider_name, wait_time_seconds).
        If provider is None, it means both are rate-limited and the system must sleep.
        """
        with self.lock:
            now = time.time()
            
            for provider, data in self.windows.items():
                self._clean_window(data["minute"])
                # Reset daily counters every 24 hours
                if now - data["day_start"] > 86400:
                    data["day"] = 0
                    data["day_start"] = now

            # 1. Try Gemini First
            gemini_data = self.windows["gemini"]
            if len(gemini_data["minute"]) < self.gemini_limit_rpm and gemini_data["day"] < self.gemini_limit_rpd:
                gemini_data["minute"].append(now)
                gemini_data["day"] += 1
                return "gemini", 0

            # 2. Fallback to Groq
            groq_data = self.windows["groq"]
            if len(groq_data["minute"]) < self.groq_limit_rpm and groq_data["day"] < self.groq_limit_rpd:
                groq_data["minute"].append(now)
                groq_data["day"] += 1
                return "groq", 0

            # 3. Both Limits Hit -> Calculate Sleep Time
            sleep_times = []
            if gemini_data["minute"]:
                sleep_times.append(60 - (now - gemini_data["minute"][0]))
            if groq_data["minute"]:
                sleep_times.append(60 - (now - groq_data["minute"][0]))
                
            sleep_duration = max(sleep_times) + 1.0 # Add 1s buffer to be safe
            return None, sleep_duration

# Singleton instance
router = ProviderRouter()