#!/usr/bin/env python3
"""
Script to check links in Jupyter notebook markdown cells.
Reports 404 errors and redirects, and creates GitHub issues for broken links.
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
import urllib.error
from typing import List, Tuple, Dict, Optional

# Try to import requests for GitHub API, but make it optional for local testing
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("Warning: requests library not found. GitHub issue creation will be disabled.", file=sys.stderr)


def extract_links_from_markdown(text: str) -> List[str]:
    """Extract all URLs from markdown text."""
    links = []
    
    # Pattern for markdown links: [text](url)
    markdown_link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    matches = re.findall(markdown_link_pattern, text)
    for _, url in matches:
        links.append(url)
    
    # Pattern for plain URLs (http:// or https://)
    url_pattern = r'https?://[^\s\)]+'
    url_matches = re.findall(url_pattern, text)
    links.extend(url_matches)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    
    return unique_links


def check_url(url: str, timeout: int = 10) -> Tuple[str, int, str]:
    """
    Check if a URL is accessible.
    Returns: (status, status_code, final_url)
    - status: 'ok', '404', 'redirect', 'error'
    - status_code: HTTP status code
    - final_url: final URL after redirects
    """
    try:
        # Create a request with redirect handling
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; LinkChecker/1.0)')
        
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
            status_code = response.getcode()
            final_url = response.geturl()
            
            # Check if redirected
            if final_url != url:
                return ('redirect', status_code, final_url)
            
            # Check for 404
            if status_code == 404:
                return ('404', status_code, final_url)
            
            # Check for other error codes
            if status_code >= 400:
                return ('error', status_code, final_url)
            
            return ('ok', status_code, final_url)
            
        except urllib.error.HTTPError as e:
            status_code = e.code
            final_url = e.geturl()
            
            if status_code == 404:
                return ('404', status_code, final_url)
            elif status_code in (301, 302, 303, 307, 308):
                # Try to follow redirect
                location = e.headers.get('Location')
                if location:
                    return ('redirect', status_code, location)
                return ('redirect', status_code, final_url)
            else:
                return ('error', status_code, final_url)
                
    except urllib.error.URLError as e:
        return ('error', 0, str(e.reason))
    except Exception as e:
        return ('error', 0, str(e))


def check_notebook_links(notebook_path: Path) -> List[Dict]:
    """Check all links in a notebook file."""
    issues = []
    
    try:
        with open(notebook_path, 'r', encoding='utf-8') as f:
            notebook = json.load(f)
    except Exception as e:
        print(f"Error reading {notebook_path}: {e}", file=sys.stderr)
        return issues
    
    # Iterate through cells
    for cell_idx, cell in enumerate(notebook.get('cells', [])):
        if cell.get('cell_type') == 'markdown':
            source = ''.join(cell.get('source', []))
            links = extract_links_from_markdown(source)
            
            for link in links:
                # Skip local file links
                parsed = urlparse(link)
                if not parsed.scheme or parsed.scheme in ('file', ''):
                    continue
                
                # Check the URL
                status, status_code, info = check_url(link)
                
                if status in ('404', 'redirect', 'error'):
                    issues.append({
                        'notebook': str(notebook_path),
                        'cell': cell_idx,
                        'url': link,
                        'status': status,
                        'status_code': status_code,
                        'info': info
                    })
    
    return issues


class GitHubIssueManager:
    """Manages GitHub issues for broken links."""
    
    def __init__(self, token: Optional[str] = None, repository: Optional[str] = None):
        self.token = token or os.getenv('GITHUB_TOKEN')
        self.repository = repository or os.getenv('GITHUB_REPOSITORY')
        self.api_base = 'https://api.github.com'
        self.session = None
        
        if HAS_REQUESTS and self.token and self.repository:
            self.session = requests.Session()
            self.session.headers.update({
                'Authorization': f'token {self.token}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'LinkChecker/1.0'
            })
    
    def get_existing_issues(self) -> Dict[str, int]:
        """Get all existing issues with label 'broken-link' and return a mapping of URL to issue number."""
        if not self.session or not self.repository:
            return {}
        
        url_to_issue = {}
        page = 1
        per_page = 100
        
        try:
            while True:
                issues_url = f'{self.api_base}/repos/{self.repository}/issues'
                params = {
                    'state': 'open',
                    'labels': 'broken-link',
                    'per_page': per_page,
                    'page': page
                }
                
                response = self.session.get(issues_url, params=params)
                response.raise_for_status()
                issues = response.json()
                
                if not issues:
                    break
                
                for issue in issues:
                    # Extract URL from issue body
                    body = issue.get('body', '')
                    url_match = re.search(r'\*\*URL:\*\* `([^`]+)`', body)
                    if url_match:
                        url = url_match.group(1)
                        url_to_issue[url] = issue['number']
                
                if len(issues) < per_page:
                    break
                page += 1
                
        except Exception as e:
            print(f"Error fetching existing issues: {e}", file=sys.stderr)
        
        return url_to_issue
    
    def create_issue(self, issue_data: Dict, existing_issues: Optional[Dict[str, int]] = None) -> Optional[int]:
        """Create a GitHub issue for a broken link. Returns issue number if successful."""
        if not self.session or not self.repository:
            return None
        
        # Check if issue already exists (use provided cache or fetch)
        if existing_issues is None:
            existing_issues = self.get_existing_issues()
        
        if issue_data['url'] in existing_issues:
            issue_num = existing_issues[issue_data['url']]
            print(f"  Issue already exists for {issue_data['url']}: #{issue_num}")
            return issue_num
        
        # Create issue title and body
        notebook_name = Path(issue_data['notebook']).name
        url_short = issue_data['url']
        if len(url_short) > 60:
            url_short = url_short[:57] + '...'
        title = f"Broken link: {url_short}"
        
        body = f"""## Broken Link Detected

**Status:** {issue_data['status'].upper()}
**URL:** `{issue_data['url']}`
**Notebook:** `{notebook_name}`
**Cell:** {issue_data['cell']}

"""
        
        if issue_data['status_code']:
            body += f"**HTTP Status Code:** {issue_data['status_code']}\n\n"
        
        if issue_data['info']:
            body += f"**Additional Info:** {issue_data['info']}\n\n"
        
        body += f"""---

This issue was automatically created by the link checker workflow.
Please verify the link and update the notebook if necessary.
"""
        
        issue_data_payload = {
            'title': title,
            'body': body,
            'labels': ['broken-link', 'automated']
        }
        
        try:
            create_url = f'{self.api_base}/repos/{self.repository}/issues'
            response = self.session.post(create_url, json=issue_data_payload)
            response.raise_for_status()
            issue = response.json()
            print(f"  Created issue #{issue['number']} for {issue_data['url']}")
            return issue['number']
        except Exception as e:
            print(f"  Error creating issue for {issue_data['url']}: {e}", file=sys.stderr)
            return None


def main():
    """Main function to check all notebooks."""
    # Look for notebooks in css04-networks-code directory
    # Try multiple possible locations
    possible_dirs = [
        Path.cwd() / 'css04-networks-code',  # From repo root
        Path(__file__).parent.parent / 'css04-networks-code',  # From .github/workflows/
        Path(__file__).parent,  # If script is in css04-networks-code/
        Path.cwd(),  # Current directory as fallback
    ]
    
    notebook_dir = None
    for dir_path in possible_dirs:
        if dir_path.exists() and list(dir_path.glob('*.ipynb')):
            notebook_dir = dir_path
            break
    
    if not notebook_dir:
        print("Error: Could not find notebook directory.", file=sys.stderr)
        return
    
    notebook_files = list(notebook_dir.glob('*.ipynb'))
    
    if not notebook_files:
        print("No .ipynb files found in the current directory.")
        return
    
    print(f"Checking {len(notebook_files)} notebook(s)...\n")
    
    all_issues = []
    for notebook_path in sorted(notebook_files):
        print(f"Checking {notebook_path.name}...", end=' ', flush=True)
        issues = check_notebook_links(notebook_path)
        all_issues.extend(issues)
        print(f"found {len(issues)} issue(s)")
    
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    
    if not all_issues:
        print("\n✓ All links are working correctly!")
        return
    
    # Group by status
    by_status = {}
    for issue in all_issues:
        status = issue['status']
        if status not in by_status:
            by_status[status] = []
        by_status[status].append(issue)
    
    # Print summary
    print(f"\nTotal issues found: {len(all_issues)}")
    for status in ['404', 'redirect', 'error']:
        if status in by_status:
            print(f"  {status.upper()}: {len(by_status[status])}")
    
    # Print detailed report
    print("\n" + "="*80)
    print("DETAILED REPORT")
    print("="*80)
    
    for issue in all_issues:
        print(f"\nNotebook: {issue['notebook']}")
        print(f"  Cell: {issue['cell']}")
        print(f"  URL: {issue['url']}")
        print(f"  Status: {issue['status'].upper()}")
        if issue['status_code']:
            print(f"  HTTP Code: {issue['status_code']}")
        if issue['info']:
            print(f"  Info: {issue['info']}")
    
    # Create GitHub issues if configured
    print("\n" + "="*80)
    print("GITHUB ISSUES")
    print("="*80)
    
    github_manager = GitHubIssueManager()
    if github_manager.session:
        print("\nFetching existing issues...")
        existing_issues = github_manager.get_existing_issues()
        print(f"Found {len(existing_issues)} existing issue(s) for broken links")
        
        print("\nCreating GitHub issues for broken links...")
        for issue in all_issues:
            github_manager.create_issue(issue, existing_issues)
    else:
        print("\nGitHub issue creation skipped (not configured or running locally)")


if __name__ == '__main__':
    main()