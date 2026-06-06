"""Tool for generating tests for code."""

from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field
from config.configuration import Config
from tools.tool_interface import Tool, ToolInvocation, ToolResult, ToolKind


class TestGeneratorParams(BaseModel):
    code: str = Field(..., description="The code to generate tests for")
    language: str = Field(..., description="Programming language (python, javascript, typescript, go, rust)")
    framework: Optional[str] = Field(None, description="Testing framework (pytest, jest, mocha, etc.)")
    file_path: Optional[str] = Field(None, description="Optional: Path where the test file should be saved")


class TestGeneratorTool(Tool):
    name = "generate_tests"
    description = """Generate unit tests for code. Creates comprehensive test cases covering normal functionality, edge cases, error conditions, and type validation."""
    kind = ToolKind.WRITE
    schema = TestGeneratorParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = TestGeneratorParams(**invocation.params)

        # Auto-detect framework if not provided
        if not params.framework:
            framework_map = {
                "python": "pytest",
                "javascript": "jest",
                "typescript": "jest",
                "go": "testing",
                "rust": "cargo test",
            }
            params.framework = framework_map.get(params.language, "pytest")

        # Generate test prompt for the AI
        test_prompt = f"""Generate comprehensive unit tests for this {params.language} code using {params.framework}.

Code to test:
```{params.language}
{params.code}
```

Generate ONLY the test code, properly formatted and ready to run."""

        return ToolResult.success_result(
            output=f"Ready to generate tests for {params.language} code using {params.framework}. Please provide the test code.",
            metadata={
                "language": params.language,
                "framework": params.framework,
                "file_path": params.file_path,
            },
        )

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return "file_path" in params and params["file_path"] is not None
