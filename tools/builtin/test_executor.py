"""Tool for executing tests."""

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field
from config.configuration import Config
from tools.tool_interface import Tool, ToolInvocation, ToolResult, ToolKind


class TestExecutorParams(BaseModel):
    test_file: Optional[str] = Field(None, description="Path to test file to execute")
    test_code: Optional[str] = Field(None, description="Test code to execute (if no file provided)")
    language: str = Field(..., description="Programming language (python, javascript, typescript)")
    framework: Optional[str] = Field(None, description="Testing framework (pytest, jest, etc.)")
    source_file: Optional[str] = Field(None, description="Path to source file being tested")


class TestExecutorTool(Tool):
    name = "run_tests"
    description = """Execute test files and return results. Runs tests using the appropriate test runner."""
    kind = ToolKind.SHELL
    schema = TestExecutorParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = TestExecutorParams(**invocation.params)

        if not params.test_file and not params.test_code:
            return ToolResult.error_result("Either test_file or test_code must be provided")

        # Auto-detect framework
        if not params.framework:
            framework_map = {
                "python": "pytest",
                "javascript": "jest",
                "typescript": "jest",
            }
            params.framework = framework_map.get(params.language, "pytest")

        try:
            # If test_code provided, create temp file
            test_file = params.test_file
            if params.test_code and not test_file:
                test_file = self._create_temp_test_file(params.test_code, params.language)

            # Execute tests
            result = self._run_tests(test_file, params.language, params.framework, invocation.cwd)
            return result

        except Exception as e:
            return ToolResult.error_result(f"Test execution failed: {str(e)}")

    def _create_temp_test_file(self, test_code: str, language: str) -> str:
        extensions = {"python": ".py", "javascript": ".js", "typescript": ".ts"}
        ext = extensions.get(language, ".py")

        with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False) as f:
            f.write(test_code)
            return f.name

    def _run_tests(self, test_file: str, language: str, framework: str, cwd: Path) -> ToolResult:
        # Build command
        if language == "python":
            cmd = ["pytest", test_file, "-v", "--tb=short"] if framework == "pytest" else ["python", "-m", "unittest", test_file]
        elif language in ["javascript", "typescript"]:
            cmd = ["npx", framework, test_file]
        else:
            return ToolResult.error_result(f"Unsupported language: {language}")

        # Run tests
        try:
            result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            success = result.returncode == 0

            if success:
                return ToolResult.success_result(output=f"✅ All tests passed!\n\n{output}")
            else:
                return ToolResult.success_result(output=f"❌ Some tests failed\n\n{output}")

        except subprocess.TimeoutExpired:
            return ToolResult.error_result("Tests timed out after 30 seconds")
        except FileNotFoundError:
            return ToolResult.error_result(f"Test runner '{framework}' not found. Install it first.")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return False
