"""Benchmarks for %s."""
import timeit, tempfile, shutil, json

def run_benchmarks():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("mod", "%s.py" % (".".join(["tiny_"+p if p!="tiny-task" else "tiny_task","tiny_"+p if p!="tiny-retry" else "tiny_retry","tiny_"+p if p!="tiny-mcp-gateway" else "tiny_mcp_gateway"][0])))
    
    # Dynamic import based on repo
    repo = "%s" % {"tiny-task":"tiny_task","tiny-retry":"tiny_retry","tiny-mcp-gateway":"tiny_mcp_gateway"}["%s" % {"tiny-task":"tiny-task","tiny-retry":"tiny-retry","tiny-mcp-gateway":"tiny-mcp-gateway"}]
    pass

if __name__ == "__main__":
    print("Run: python -m pytest benchmark.py")
