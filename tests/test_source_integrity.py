import ast
import unittest
from pathlib import Path


class SourceIntegrityTests(unittest.TestCase):
    def test_classes_do_not_define_duplicate_methods(self) -> None:
        root = Path(__file__).resolve().parents[1]
        duplicates = []

        for path in sorted((root / "src").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods = {}
                for child in node.body:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    methods.setdefault(child.name, []).append(child.lineno)
                for name, lines in methods.items():
                    if len(lines) > 1:
                        duplicates.append(f"{path.name}:{node.name}.{name} at {lines}")

        self.assertEqual([], duplicates, "Duplicate class methods found: " + "; ".join(duplicates))


if __name__ == "__main__":
    unittest.main()
