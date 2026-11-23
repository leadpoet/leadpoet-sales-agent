"""File tool schemas from the Poet9B workflow harness."""
FILE_TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "List the files in the workspace.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file in the workspace.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                     "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file",
                                      "description": "Create or replace a text file in the workspace.",
                                      "parameters": {"type": "object", "properties": {
                                          "path": {"type": "string"}, "content": {"type": "string"}},
                                          "required": ["path", "content"]}}},
]
