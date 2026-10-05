"""Build all C++17 targets and reject sanitizer reports, including child logs."""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]


def execute(command, log, environment):
    with log.open("w", encoding="utf-8") as output:
        output.write(json.dumps(command) + "\n")
        output.flush()
        process = subprocess.Popen(command, cwd=ROOT, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace")
        for line in process.stdout:
            print(line, end="", flush=True)
            output.write(line)
        return process.wait()


def sanitizer_environment(directory):
    environment = os.environ.copy()
    environment["ASAN_OPTIONS"] = (
        "halt_on_error=1:detect_leaks=1:log_path=" + str(directory / "asan"))
    environment["UBSAN_OPTIONS"] = (
        "halt_on_error=1:print_stacktrace=1:log_path=" + str(directory / "ubsan"))
    return environment


def reports(directory):
    return [path for prefix in ("asan.*", "ubsan.*")
            for path in directory.glob(prefix) if path.stat().st_size]


def verify_detectors(build, evidence):
    probes = evidence / "detector-probes"
    probes.mkdir()
    environment = sanitizer_environment(probes)
    cache = (build / "CMakeCache.txt").read_text(encoding="utf-8")
    compiler = next(line.split("=", 1)[1] for line in cache.splitlines()
                    if line.startswith("CMAKE_CXX_COMPILER:FILEPATH="))
    cases = {
        "asan": ("int main(){int* p=new int[1];delete[] p;return *p;}", "heap-use-after-free"),
        "ubsan": ("#include <climits>\nint main(){volatile int x=INT_MAX;return x+1;}",
                  "signed integer overflow"),
    }
    for name, (source, marker) in cases.items():
        source_path, executable = probes / (name + ".cpp"), probes / (name + "-probe")
        source_path.write_text(source, encoding="utf-8")
        command = [compiler, "-std=c++17", "-fsanitize=" + ("address" if name == "asan" else "undefined"),
                   "-fno-sanitize-recover=all", "-fno-omit-frame-pointer", "-g", "-O0",
                   str(source_path), "-o", str(executable)]
        if execute(command, probes / (name + "-build.log"), environment):
            raise RuntimeError(name + " detector probe did not compile")
        # Real test scripts can discard stderr; log_path must still catch errors.
        result = subprocess.run([str(executable)], env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        emitted = "\n".join(path.read_text(errors="replace") for path in probes.glob(name + ".*"))
        if result.returncode == 0 or marker not in emitted:
            raise RuntimeError(name + " detector probe failed to report its intentional error")
        print(name.upper() + " detector and child-log capture verified", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", default="build-sanitizers")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--enable-test-faults", action="store_true")
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or args.parallel < 1:
        parser.error("run in Linux with --parallel >= 1 (Docker is supported)")
    build = (ROOT / args.build_dir).resolve()
    evidence = build / "sanitizer-runs" / (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    environment = sanitizer_environment(evidence)
    summary = {"cxx_standard": 17, "build": str(build), "evidence": str(evidence),
               "test_faults": args.enable_test_faults, "commands": [], "passed": False}
    try:
        commands = [
            ("configure", ["cmake", "-S", str(ROOT), "-B", str(build), "-G", "Ninja",
                           "-DCMAKE_BUILD_TYPE=Debug", "-DARENA_ENABLE_SANITIZERS=ON",
                           "-DARENA_ENABLE_TEST_FAULTS=" + ("ON" if args.enable_test_faults else "OFF"),
                           "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]),
            ("build", ["cmake", "--build", str(build), "--parallel", str(args.parallel)]),
        ]
        for name, command in commands:
            code = execute(command, evidence / (name + ".log"), environment)
            summary["commands"].append({"name": name, "exit_code": code})
            if code:
                raise RuntimeError(name + " failed")
        compilation = json.loads((build / "compile_commands.json").read_text())
        if not compilation or any("-fsanitize=address,undefined" not in item["command"] or
                                  "-std=c++17" not in item["command"] for item in compilation):
            raise RuntimeError("some C++ compilation units lack C++17 or sanitizer instrumentation")
        summary["instrumented_units"] = len(compilation)
        verify_detectors(build, evidence)
        summary["detector_probes_passed"] = True
        code = execute(["ctest", "--test-dir", str(build), "--output-on-failure",
                        "--parallel", str(args.parallel), "--output-junit", str(evidence / "ctest.xml")],
                       evidence / "ctest.log", environment)
        summary["commands"].append({"name": "ctest", "exit_code": code})
        findings = reports(evidence)
        summary["sanitizer_reports"] = [str(path) for path in findings]
        if code or findings:
            raise RuntimeError("CTest failed or a child process emitted a sanitizer report")
        summary["passed"] = True
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        summary["error"] = str(error)
        print("Sanitizer verification failed: " + str(error), file=sys.stderr)
        return 1
    finally:
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("Evidence: " + str(evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
