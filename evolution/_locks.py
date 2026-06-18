"""全局文件写入锁，防止并发数据丢失

按资源类型分锁，避免不必要的互斥：
  asset_lock     — assets/*.md 写入（save_asset）
  log_lock       — logs/*.jsonl 写入（LogCollector._append）
  discovery_lock — discoveries.json 写入（PatternDiscovery._save）
  rules_lock     — pending_rules.json 写入（RuleGenerator._save）
  results_lock   — results.tsv 写入（_write_results_tsv）
  print_lock     — 多线程日志输出（_log）
"""
import threading

asset_lock = threading.Lock()
log_lock = threading.Lock()
discovery_lock = threading.Lock()
rules_lock = threading.Lock()
results_lock = threading.Lock()
print_lock = threading.Lock()
