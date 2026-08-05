from deepagents import create_deep_agent

from repo_cartographer.models import model
from repo_cartographer.tools import get_file_contents, get_repo_tree, search_code

ORCHESTRATOR_PROMPT = """You are an AI agent that helps developers by explaining code based on their requests. You have access to a code repository and can read the code files to understand the context."""

# You can also use the GitHub API to fetch the contents of specific files in the repository. The contents of the files are returned in base64 encoding, which you can decode to get the actual content as a string.
# You can use the following functions to interact with the GitHub API:
# - get_repo_tree(owner: str, repo: str, ref: str = "HEAD"): This function retrieves the file paths in a GitHub repository at a specific reference (branch, tag, or commit). It returns a list of repo-relative paths of every file in the tree. If the tree is truncated, it raises a GitHubError.
# - get_file_contents(owner: str, repo: str, path: str): This function retrieves the contents of a specific file in a GitHub repository. It returns the contents of the file as a string. If the file cannot be fetched, it raises a GitHubError.
# You can use these functions to read the code files in the repository and understand the context of the code. You can then generate code based on the developer's requests, taking into account the existing code and any relevant information from the repository."""

agent = create_deep_agent(
    name="repo_cartographer",
    system_prompt=ORCHESTRATOR_PROMPT,
    model=model,
    # Passed as plain functions: LangChain derives each tool's schema and
    # description from its signature and docstring.
    tools=[get_repo_tree, get_file_contents, search_code],
)

if __name__ == "__main__":
    # Example usage: Explain the purpose of the get_repo_tree function
    result = agent.invoke({"messages": [{"role": "user", "content": "Please explain the purpose of the get_repo_tree function in repo_cartographer/tools.py."}]})
    
    print(result["messages"][-1].content)
