"""
Confluence Content Preprocessor
Preserves exact logic from notebook for content extraction and processing
"""

import html2text
import re
from typing import List, Dict, Optional, Callable
from bs4 import BeautifulSoup
import logging
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Cache for user display names to avoid repeated API calls
_user_name_cache: Dict[str, str] = {}


def clear_user_cache():
    """Clear the user display name cache."""
    global _user_name_cache
    _user_name_cache = {}
    logger.info("User display name cache cleared")


def get_user_cache_stats() -> Dict:
    """Get statistics about the user cache."""
    return {
        'cached_users': len(_user_name_cache),
        'user_ids': list(_user_name_cache.keys())
    }


class ConfluencePreprocessor:
    """Advanced preprocessing for Confluence HTML content"""
    
    def __init__(self, 
                 confluence_base_url: str,
                 preserve_links: bool = True,
                 preserve_images: bool = True,
                 extract_tables: bool = True,
                 extract_code_blocks: bool = True,
                 confluence_client = None):
        
        self.confluence_base_url = confluence_base_url.rstrip('/')
        self.confluence_client = confluence_client  # Optional: for fetching user display names
        
        # Configure html2text with custom settings
        self.h2t = html2text.HTML2Text()
        self.h2t.ignore_links = not preserve_links
        self.h2t.ignore_images = not preserve_images
        self.h2t.ignore_emphasis = False
        self.h2t.body_width = 0  # No text wrapping
        self.h2t.single_line_break = False
        self.h2t.mark_code = True
        self.h2t.wrap_links = False
        
        # Regex patterns for Confluence-specific elements
        self.patterns = {
            'confluence_macro': re.compile(r'<ac:structured-macro.*?</ac:structured-macro>', re.DOTALL),
            'confluence_placeholder': re.compile(r'<ac:placeholder>(.*?)</ac:placeholder>', re.DOTALL),
            'user_mention': re.compile(r'<ac:link.*?ri:user.*?</ac:link>', re.DOTALL),
            'page_link': re.compile(r'<ac:link.*?ri:page.*?</ac:link>', re.DOTALL),
            'space_link': re.compile(r'<ac:link.*?ri:space.*?</ac:link>', re.DOTALL),
            'task_list': re.compile(r'<ac:task-list.*?</ac:task-list>', re.DOTALL),
            'expand_macro': re.compile(r'<ac:structured-macro ac:name="expand".*?</ac:structured-macro>', re.DOTALL),
            'info_macro': re.compile(r'<ac:structured-macro ac:name="info".*?</ac:structured-macro>', re.DOTALL),
            'warning_macro': re.compile(r'<ac:structured-macro ac:name="warning".*?</ac:structured-macro>', re.DOTALL),
            'code_macro': re.compile(r'<ac:structured-macro ac:name="code".*?</ac:structured-macro>', re.DOTALL),
            'panel_macro': re.compile(r'<ac:structured-macro ac:name="panel".*?</ac:structured-macro>', re.DOTALL),
            # JIRA ticket pattern (e.g., PROJ-123, ABC-456)
            'jira_ticket': re.compile(r'\b([A-Z]{2,10}-\d+)\b'),
        }
        
        # Table extraction settings
        self.extract_tables = extract_tables
        self.extract_code_blocks = extract_code_blocks
    
    def _get_table_context(self, table_element) -> str:
        """
        Extract context/title for a table by looking at preceding elements.
        Looks for:
        1. Preceding header (h1-h6)
        2. Preceding paragraph with bold text
        3. Table caption
        4. Previous sibling text content
        """
        context = ""
        
        try:
            # Check for table caption
            caption = table_element.find('caption')
            if caption:
                return caption.get_text(strip=True)
            
            # Look for preceding header
            prev_sibling = table_element.find_previous_sibling()
            for _ in range(5):  # Look up to 5 siblings back
                if prev_sibling is None:
                    break
                    
                # Check if it's a header
                if prev_sibling.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                    context = prev_sibling.get_text(strip=True)
                    break
                
                # Check for paragraph with strong/bold text
                if prev_sibling.name == 'p':
                    strong = prev_sibling.find(['strong', 'b'])
                    if strong:
                        context = strong.get_text(strip=True)
                        break
                    # Or if paragraph is short (likely a label)
                    text = prev_sibling.get_text(strip=True)
                    if len(text) < 100 and text:
                        context = text
                        break
                
                # Check for div with title-like content
                if prev_sibling.name == 'div':
                    header_in_div = prev_sibling.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b'])
                    if header_in_div:
                        context = header_in_div.get_text(strip=True)
                        break
                
                prev_sibling = prev_sibling.find_previous_sibling()
            
            # Also check parent for context (tables nested in panels/sections)
            if not context:
                parent = table_element.parent
                for _ in range(3):  # Check up to 3 parents
                    if parent is None:
                        break
                    # Look for ac:parameter with name="title" (Confluence panels)
                    title_param = parent.find('ac:parameter', {'ac:name': 'title'})
                    if title_param:
                        context = title_param.get_text(strip=True)
                        break
                    parent = parent.parent
                    
        except Exception as e:
            logger.debug(f"Error extracting table context: {e}")
        
        return context
    
    def _extract_confluence_macros(self, html_content: str) -> Dict[str, List[str]]:
        """Extract and process Confluence-specific macros"""
        extracted = {
            'code_blocks': [],
            'tables': [],
            'panels': [],
            'warnings': [],
            'info_boxes': [],
            'expands': [],
            'jira_links': [],
            'external_links': [],
            'internal_links': []
        }
        
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Extract code blocks
            code_macros = soup.find_all('ac:structured-macro', {'ac:name': 'code'})
            for code in code_macros:
                language = code.find('ac:parameter', {'ac:name': 'language'})
                lang = language.text if language else 'text'
                code_body = code.find('ac:plain-text-body')
                if code_body:
                    code_text = code_body.text
                    extracted['code_blocks'].append(f"```{lang}\n{code_text}\n```")
            
            # Extract JIRA issue macros - MUST happen before text extraction
            # These are Confluence macros like <ac:structured-macro ac:name="jira" ...>
            # They contain key, server, and other parameters
            jira_macros = soup.find_all('ac:structured-macro', {'ac:name': 'jira'})
            for jira_macro in jira_macros:
                # Get the JIRA key (ticket ID like LH-3760)
                key_param = jira_macro.find('ac:parameter', {'ac:name': 'key'})
                server_param = jira_macro.find('ac:parameter', {'ac:name': 'server'})
                
                if key_param and key_param.text:
                    ticket_id = key_param.text.strip()
                    server_name = server_param.text.strip() if server_param else 'JIRA'
                    
                    # Build JIRA URL based on server name
                    # Common Jira patterns
                    jira_url = None
                    if 'lilly' in server_name.lower():
                        jira_url = f"https://lilly-jira.atlassian.net/browse/{ticket_id}"
                    elif 'welldoc' in server_name.lower():
                        jira_url = f"https://welldoc.atlassian.net/browse/{ticket_id}"
                    else:
                        # Try to use a generic Atlassian URL
                        jira_url = f"https://jira.atlassian.net/browse/{ticket_id}"
                    
                    # Create markdown link
                    jira_link = f"[{ticket_id}]({jira_url})"
                    
                    # Store in extracted jira_links
                    if jira_link not in extracted['jira_links']:
                        extracted['jira_links'].append(jira_link)
                    
                    # Replace the macro element with a clean markdown link
                    # This prevents the h2t.handle() from creating garbage like "LH-3760f40b3454...System Jira"
                    jira_macro.replace_with(jira_link)
                else:
                    # If no key found, just remove the macro to prevent garbage output
                    jira_macro.decompose()
            
            # Extract panels and info boxes
            for macro_name, container in [('panel', 'panels'), 
                                         ('info', 'info_boxes'),
                                         ('warning', 'warnings'),
                                         ('expand', 'expands')]:
                macros = soup.find_all('ac:structured-macro', {'ac:name': macro_name})
                for macro in macros:
                    title = macro.find('ac:parameter', {'ac:name': 'title'})
                    title_text = f"**{title.text}**\n" if title else ""
                    content = macro.find('ac:rich-text-body')
                    if content:
                        macro_text = self.h2t.handle(str(content))
                        extracted[container].append(f"{title_text}{macro_text}")
            
            # Extract tables with enhanced context preservation
            if self.extract_tables:
                tables = soup.find_all('table')
                for table_idx, table in enumerate(tables):
                    rows = table.find_all('tr')
                    if rows:
                        # Get table context: preceding header or title
                        table_title = self._get_table_context(table)
                        
                        # Extract headers from first row
                        header_row = rows[0]
                        header_cells = header_row.find_all(['td', 'th'])
                        headers = [cell.get_text(strip=True) for cell in header_cells]
                        
                        # Build both markdown table AND row-by-row representation
                        table_md = []
                        row_by_row = []
                        
                        # Add table title if found
                        if table_title:
                            table_md.append(f"**Table: {table_title}**\n")
                            row_by_row.append(f"=== TABLE: {table_title} ===")
                        
                        # Add markdown header row
                        table_md.append('| ' + ' | '.join(headers) + ' |')
                        table_md.append('|' + '---|' * len(headers))
                        
                        # Process data rows
                        for i, row in enumerate(rows[1:], 1):
                            cells = row.find_all(['td', 'th'])
                            cell_texts = [cell.get_text(strip=True) for cell in cells]
                            
                            # Markdown format
                            table_md.append('| ' + ' | '.join(cell_texts) + ' |')
                            
                            # Row-by-row format for better LLM comprehension
                            # Format: "Row N - Column1: value1, Column2: value2, ..."
                            if cell_texts and any(ct.strip() for ct in cell_texts):
                                row_parts = []
                                for h_idx, header in enumerate(headers):
                                    if h_idx < len(cell_texts) and cell_texts[h_idx].strip():
                                        row_parts.append(f"{header}: {cell_texts[h_idx]}")
                                if row_parts:
                                    row_context = f"Row {i}: " + " | ".join(row_parts)
                                    row_by_row.append(row_context)
                        
                        if table_md:
                            # Combine markdown table with row-by-row representation
                            full_table_content = '\n'.join(table_md)
                            if row_by_row:
                                full_table_content += "\n\n--- Row-by-Row Details ---\n"
                                full_table_content += '\n'.join(row_by_row)
                            extracted['tables'].append(full_table_content)
            
            # Extract JIRA links and external links
            # JIRA links can appear as:
            # 1. Regular <a> tags with href to JIRA instances
            # 2. JIRA ticket IDs in text (e.g., PROJ-123)
            # 3. Confluence <ac:link> tags
            
            # Extract all <a> tags (external links and JIRA links)
            all_links = soup.find_all('a')
            for link in all_links:
                href = link.get('href', '')
                link_text = link.get_text(strip=True)
                
                if href:
                    # Check if it's a JIRA link (common JIRA patterns)
                    if any(pattern in href.lower() for pattern in ['jira', 'atlassian.net/browse/', '/browse/']):
                        jira_info = f"[{link_text}]({href})"
                        if jira_info not in extracted['jira_links']:
                            extracted['jira_links'].append(jira_info)
                    # External links (http/https)
                    elif href.startswith('http://') or href.startswith('https://'):
                        external_info = f"[{link_text}]({href})"
                        if external_info not in extracted['external_links']:
                            extracted['external_links'].append(external_info)
            
            # Extract Confluence internal links (<ac:link>)
            confluence_links = soup.find_all('ac:link')
            for link in confluence_links:
                # Skip user mentions (already handled)
                if link.find('ri:user'):
                    continue
                
                link_body = link.find('ac:link-body')
                if link_body:
                    link_text = link_body.get_text(strip=True)
                    
                    # Check for page links
                    page_ref = link.find('ri:page')
                    if page_ref:
                        page_title = page_ref.get('ri:content-title', '')
                        if page_title:
                            internal_info = f"Internal Link: {link_text} → {page_title}"
                            if internal_info not in extracted['internal_links']:
                                extracted['internal_links'].append(internal_info)
            
            # Extract JIRA ticket IDs from text content
            text_content = soup.get_text()
            jira_tickets = self.patterns['jira_ticket'].findall(text_content)
            if jira_tickets:
                # Deduplicate and add unique JIRA tickets
                unique_tickets = list(set(jira_tickets))
                for ticket in unique_tickets:
                    ticket_ref = f"JIRA Ticket: {ticket}"
                    if ticket_ref not in extracted['jira_links']:
                        extracted['jira_links'].append(ticket_ref)
            
        except Exception as e:
            logger.warning(f"Error extracting macros: {e}")
        
        return extracted
    
    def _get_user_display_name(self, user_id: str) -> Optional[str]:
        """
        Fetch user display name from Confluence API using account ID.
        Uses caching to avoid repeated API calls.
        
        Args:
            user_id: The user's account ID or userkey
            
        Returns:
            Display name if found, None otherwise
        """
        global _user_name_cache
        
        # Check cache first (includes negative cache for failed lookups)
        if user_id in _user_name_cache:
            cached = _user_name_cache[user_id]
            # Return None for negative cache entries
            return cached if cached != '__NOT_FOUND__' else None
        
        if not self.confluence_client or not user_id:
            return None
        
        try:
            # Try to get user by account ID (Confluence Cloud)
            # The atlassian-python-api uses different methods depending on version
            user_info = None
            
            # Method 1: Try get_user_details_by_accountid (newer API)
            if hasattr(self.confluence_client, 'get_user_details_by_accountid'):
                try:
                    user_info = self.confluence_client.get_user_details_by_accountid(user_id)
                except Exception:
                    pass
            
            # Method 2: Try direct REST API call (only if user_id looks like account ID)
            if not user_info and len(user_id) < 50:  # Account IDs are typically short
                try:
                    user_info = self.confluence_client.get(
                        f'/wiki/rest/api/user?accountId={user_id}',
                        absolute=False
                    )
                    # Validate response is a dict (not HTML error page)
                    if not isinstance(user_info, dict):
                        user_info = None
                except Exception:
                    pass
            
            # Method 3: Try legacy userkey endpoint (skip if already tried as account ID)
            if not user_info:
                try:
                    user_info = self.confluence_client.get(
                        f'/rest/api/user?key={user_id}',
                        absolute=False
                    )
                    # Validate response is a dict (not HTML error page)
                    if not isinstance(user_info, dict):
                        user_info = None
                except Exception:
                    pass
            
            if user_info and isinstance(user_info, dict):
                display_name = user_info.get('displayName') or user_info.get('publicName')
                if display_name:
                    _user_name_cache[user_id] = display_name
                    logger.debug(f"Fetched user display name: {user_id} -> {display_name}")
                    return display_name
            
            # Cache negative result to avoid repeated API calls
            _user_name_cache[user_id] = '__NOT_FOUND__'
        
        except Exception as e:
            # Cache negative result on error
            _user_name_cache[user_id] = '__NOT_FOUND__'
            logger.debug(f"Could not fetch user info for {user_id}: {e}")
        
        return None
    
    def _replace_user_mentions(self, html_content: str) -> str:
        """
        Replace Confluence user mention tags with actual user display names.
        
        Confluence user mentions have this structure:
        <ac:link>
          <ri:user ri:account-id="..." ri:userkey="..." />
          <ac:link-body>Display Name</ac:link-body>  (optional - may not be present!)
        </ac:link>
        
        In Confluence Cloud, the link-body is often empty and the name must be
        fetched via the API using the account-id.
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find all user mention links
            user_links = soup.find_all('ac:link')
            for link in user_links:
                # Check if this is a user mention
                user_elem = link.find('ri:user')
                if user_elem:
                    display_name = None
                    
                    # Try 1: Get the display name from link-body (may contain formatted text)
                    link_body = link.find('ac:link-body')
                    if link_body:
                        display_name = link_body.get_text(strip=True)
                    
                    # Try 2: Get from plain-text-link-body
                    if not display_name:
                        plain_link_body = link.find('ac:plain-text-link-body')
                        if plain_link_body:
                            display_name = plain_link_body.get_text(strip=True)
                    
                    # Try 3: Fetch from Confluence API using account ID
                    if not display_name:
                        # Try account-id first (Confluence Cloud)
                        account_id = user_elem.get('ri:account-id')
                        if account_id:
                            display_name = self._get_user_display_name(account_id)
                        
                        # Fallback to userkey (older Confluence)
                        if not display_name:
                            user_key = user_elem.get('ri:userkey')
                            if user_key:
                                display_name = self._get_user_display_name(user_key)
                    
                    # Replace the link with @DisplayName or fallback
                    if display_name:
                        link.replace_with(f'@{display_name}')
                    else:
                        # Final fallback - try to use any text content in the link
                        fallback_text = link.get_text(strip=True)
                        if fallback_text:
                            link.replace_with(f'@{fallback_text}')
                        else:
                            # Use a generic placeholder - only log at debug level to reduce noise
                            logger.debug(f"Could not resolve user mention: account-id={user_elem.get('ri:account-id')}, userkey={user_elem.get('ri:userkey')}")
                            link.replace_with('@[user]')
            
            return str(soup)
        except Exception as e:
            logger.warning(f"Error replacing user mentions: {e}")
            # Fallback to old behavior if parsing fails
            return re.sub(self.patterns['user_mention'], '@[unresolved user]', html_content)
    
    def _replace_jira_macros(self, html_content: str) -> str:
        """
        Replace Confluence JIRA macros with clean markdown links.
        
        This prevents garbage output like "LH-3760f40b3454-ac1f-3500-8963-4358af0b9acbSystem Jira"
        by properly parsing the macro and creating a clickable markdown link.
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find all JIRA macros
            jira_macros = soup.find_all('ac:structured-macro', {'ac:name': 'jira'})
            
            for jira_macro in jira_macros:
                # Get the JIRA key (ticket ID like LH-3760, RVN-16404)
                key_param = jira_macro.find('ac:parameter', {'ac:name': 'key'})
                server_param = jira_macro.find('ac:parameter', {'ac:name': 'server'})
                serverId_param = jira_macro.find('ac:parameter', {'ac:name': 'serverId'})
                
                if key_param and key_param.text:
                    ticket_id = key_param.text.strip()
                    server_name = server_param.text.strip() if server_param else 'JIRA'
                    
                    # Build JIRA URL based on server name or serverId
                    jira_url = None
                    if 'lilly' in server_name.lower() or 'system jira' in server_name.lower():
                        # System Jira typically refers to the internal Lilly Jira
                        jira_url = f"https://lilly-jira.atlassian.net/browse/{ticket_id}"
                    elif 'welldoc' in server_name.lower():
                        jira_url = f"https://welldoc.atlassian.net/browse/{ticket_id}"
                    else:
                        # Default to Welldoc as it's the most common in this workspace
                        jira_url = f"https://welldoc.atlassian.net/browse/{ticket_id}"
                    
                    # Create a proper markdown link element
                    # We'll create an anchor tag that html2text can convert properly
                    new_tag = soup.new_tag('a', href=jira_url)
                    new_tag.string = ticket_id
                    jira_macro.replace_with(new_tag)
                else:
                    # If no key found, just remove the macro to prevent garbage output
                    jira_macro.decompose()
            
            return str(soup)
        except Exception as e:
            logger.warning(f"Error replacing JIRA macros: {e}")
            return html_content
    
    def _clean_html_content(self, html_content: str) -> str:
        """Clean HTML content before conversion"""
        # Remove script and style tags
        html_content = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL)
        
        # Extract and preserve actual user names from Confluence user mentions
        html_content = self._replace_user_mentions(html_content)
        
        # Replace JIRA macros with clean markdown links BEFORE html2text conversion
        html_content = self._replace_jira_macros(html_content)
        
        # Replace line breaks
        html_content = html_content.replace('<br/>', ' ').replace('<br>', ' ')
        
        # Remove empty paragraphs
        html_content = re.sub(r'<p>\s*</p>', '', html_content)
        
        # Clean up whitespace
        html_content = re.sub(r'\s+', ' ', html_content)
        
        return html_content
    
    def _convert_html_to_markdown(self, html_content: str) -> str:
        """Convert cleaned HTML to markdown"""
        markdown = self.h2t.handle(html_content)
        markdown = self._clean_markdown(markdown)
        return markdown
    
    def _clean_markdown(self, markdown: str) -> str:
        """Clean and enhance markdown output"""
        # Remove redundant spaces
        markdown = re.sub(r'[ \t]+', ' ', markdown)
        
        # Fix list formatting
        markdown = re.sub(r'^\s*[-*+]\s+', '- ', markdown, flags=re.MULTILINE)
        
        # Fix header formatting
        markdown = re.sub(r'^#+\s+', lambda m: m.group(0).rstrip(), markdown, flags=re.MULTILINE)
        
        # Remove empty headers
        markdown = re.sub(r'^#+\s*\n', '', markdown, flags=re.MULTILINE)
        
        # FIX JIRA GARBAGE: Clean up patterns like "LH-3760f40b3454-ac1f-3500-8963-4358af0b9acbSystem Jira"
        # These are malformed JIRA macro outputs where ticket ID + UUID + server name got concatenated
        # Pattern: TICKET_ID (2-10 uppercase letters, dash, digits) followed by a UUID and optional "System Jira"
        jira_garbage_pattern = r'([A-Z]{2,10}-\d+)([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:System Jira|Jira)?'
        
        def fix_jira_garbage(match):
            ticket_id = match.group(1)
            # Return just the clean ticket ID - the actual link will be created if needed
            return ticket_id
        
        markdown = re.sub(jira_garbage_pattern, fix_jira_garbage, markdown, flags=re.IGNORECASE)
        
        # Normalize line breaks
        markdown = re.sub(r'\n{3,}', '\n\n', markdown)
        
        # Remove trailing whitespace
        markdown = '\n'.join(line.rstrip() for line in markdown.split('\n'))
        
        # Remove leading/trailing blank lines
        markdown = markdown.strip()
        
        return markdown
    
    def _extract_metadata(self, page: Dict) -> Dict:
        """Extract metadata from page object"""
        metadata = {
            'page_id': page.get('id'),
            'title': page.get('title', '').strip(),
            'space_key': page.get('space', {}).get('key', ''),
            'space_name': page.get('space', {}).get('name', ''),
            'creator': page.get('history', {}).get('createdBy', {}).get('displayName', ''),
            'created_date': page.get('history', {}).get('createdDate', ''),
            'last_modified': page.get('version', {}).get('when', ''),
            'version_number': page.get('version', {}).get('number', 1),
            'labels': ', '.join([label.get('name', '') for label in page.get('metadata', {}).get('labels', {}).get('results', [])]),
            'ancestors': ', '.join([ancestor.get('title', '') for ancestor in page.get('ancestors', [])]),
            'parent_id': page.get('parentId'),
            'parent_title': page.get('parentTitle', '')
        }
        
        # Build full URL - use _links.webui if available, otherwise construct manually
        # Confluence Cloud format: base_url/wiki + webui_path
        # Note: Confluence API returns webui as /spaces/{key}/pages/{id}/title (WITHOUT /wiki prefix)
        if page.get('_links', {}).get('webui'):
            # Use the webui link from Confluence API, but need to add /wiki prefix
            webui_path = page['_links']['webui']
            # Ensure we add /wiki prefix to the webui path
            full_path = f"/wiki{webui_path}" if not webui_path.startswith('/wiki') else webui_path
            metadata['url'] = urljoin(self.confluence_base_url, full_path)
            logger.debug(f"Using webui link: {metadata['url']}")
        else:
            # Fallback to manual construction with correct /wiki prefix
            metadata['url'] = urljoin(
                self.confluence_base_url,
                f"/wiki/spaces/{metadata['space_key']}/pages/{metadata['page_id']}"
            )
            logger.debug(f"Constructed URL: {metadata['url']}")
        
        return metadata
    
    def _assemble_content(self, markdown_content: str, extracted_macros: Dict) -> str:
        """Assemble final content with extracted elements"""
        content_parts = [markdown_content]
        
        # Add extracted code blocks
        if extracted_macros['code_blocks'] and self.extract_code_blocks:
            content_parts.append("\n## Code Examples\n")
            for i, code_block in enumerate(extracted_macros['code_blocks'], 1):
                content_parts.append(f"### Code Block {i}\n")
                content_parts.append(code_block)
                content_parts.append("")
        
        # Add extracted tables
        if extracted_macros['tables'] and self.extract_tables:
            content_parts.append("\n## Tables\n")
            for i, table in enumerate(extracted_macros['tables'], 1):
                content_parts.append(f"### Table {i}\n")
                content_parts.append(table)
                content_parts.append("")
        
        # Add panels and info boxes
        for macro_type in ['panels', 'info_boxes', 'warnings', 'expands']:
            if extracted_macros[macro_type]:
                macro_title = macro_type.replace('_', ' ').title()
                content_parts.append(f"\n## {macro_title}\n")
                for i, macro_content in enumerate(extracted_macros[macro_type], 1):
                    content_parts.append(f"### {macro_title[:-1]} {i}\n")
                    content_parts.append(macro_content)
                    content_parts.append("")
        
        # Add JIRA links
        if extracted_macros.get('jira_links'):
            content_parts.append("\n## JIRA References\n")
            for jira_link in extracted_macros['jira_links']:
                content_parts.append(f"- {jira_link}")
            content_parts.append("")
        
        # Add external links
        if extracted_macros.get('external_links'):
            content_parts.append("\n## External Links\n")
            for ext_link in extracted_macros['external_links']:
                content_parts.append(f"- {ext_link}")
            content_parts.append("")
        
        # Add internal Confluence links
        if extracted_macros.get('internal_links'):
            content_parts.append("\n## Related Pages\n")
            for int_link in extracted_macros['internal_links']:
                content_parts.append(f"- {int_link}")
            content_parts.append("")
        
        return '\n'.join(content_parts)
    
    def _calculate_content_stats(self, content: str, extracted_macros: Dict) -> Dict:
        """Calculate statistics about the content"""
        lines = content.split('\n')
        words = re.findall(r'\b\w+\b', content)
        
        return {
            'characters': len(content),
            'words': len(words),
            'lines': len(lines),
            'headers': len(re.findall(r'^#+\s', content, re.MULTILINE)),
            'code_blocks': len(extracted_macros['code_blocks']),
            'tables': len(extracted_macros['tables']),
            'panels': len(extracted_macros['panels']),
            'info_boxes': len(extracted_macros['info_boxes']),
            'warnings': len(extracted_macros['warnings']),
            'expands': len(extracted_macros['expands']),
            'jira_links': len(extracted_macros.get('jira_links', [])),
            'external_links': len(extracted_macros.get('external_links', [])),
            'internal_links': len(extracted_macros.get('internal_links', [])),
            'links': len(re.findall(r'\[.*?\]\(.*?\)', content)),
            'images': len(re.findall(r'!\[.*?\]\(.*?\)', content))
        }
    
    def process_page(self, page: Dict) -> Optional[Dict]:
        """Process a single Confluence page"""
        try:
            # Extract metadata
            metadata = self._extract_metadata(page)
            
            # Get HTML content
            html_content = page.get('body', {}).get('storage', {}).get('value', '')
            if not html_content:
                logger.warning(f"No content found for page {metadata['page_id']}")
                return None
            
            # Extract Confluence macros
            extracted_macros = self._extract_confluence_macros(html_content)
            
            # Clean HTML content
            cleaned_html = self._clean_html_content(html_content)
            
            # Convert to markdown
            markdown_content = self._convert_html_to_markdown(cleaned_html)
            
            if not markdown_content.strip():
                logger.warning(f"Empty content after conversion for page {metadata['page_id']}")
                return None
            
            # Assemble final content
            final_content = self._assemble_content(markdown_content, extracted_macros)
            
            # Calculate stats
            content_stats = self._calculate_content_stats(final_content, extracted_macros)
            
            # Create processed page
            processed_page = {
                'page_id': metadata['page_id'],
                'title': metadata['title'],
                'content': final_content,
                'space_key': metadata['space_key'],
                'space_name': metadata['space_name'],
                'url': metadata['url'],
                'version': metadata['version_number'],
                'metadata': {
                    'creator': metadata['creator'],
                    'created_date': metadata['created_date'],
                    'last_modified': metadata['last_modified'],
                    'labels': metadata['labels'],
                    'ancestors': metadata['ancestors'],
                    'parent_id': metadata['parent_id'],
                    'parent_title': metadata['parent_title'],
                    'content_stats': content_stats,
                    'jira_links': ', '.join(extracted_macros.get('jira_links', [])),
                    'external_links': ', '.join(extracted_macros.get('external_links', [])),
                    'internal_links': ', '.join(extracted_macros.get('internal_links', []))
                }
            }
            
            logger.info(f"Processed page: {metadata['title']} ({len(final_content)} chars)")
            return processed_page
            
        except Exception as e:
            logger.error(f"Error processing page {page.get('id', 'unknown')}: {str(e)}")
            return None
    
    def process_pages_batch(self, pages: List[Dict], batch_size: int = 50) -> List[Dict]:
        """Process pages in batches"""
        processed_pages = []
        
        for i in range(0, len(pages), batch_size):
            batch = pages[i:i+batch_size]
            logger.info(f"Processing batch {i//batch_size + 1}/{(len(pages)+batch_size-1)//batch_size}")
            
            for page in batch:
                processed_page = self.process_page(page)
                if processed_page:
                    processed_pages.append(processed_page)
        
        return processed_pages
