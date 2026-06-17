"""
Smart Chunker for Confluence Content
exact logic from notebook for semantic chunking
"""

import re
from typing import List, Dict, Optional, Tuple


class SmartChunker:
    """
    Advanced chunking strategy for Confluence content with robust metadata handling.
    
    Features:
    - Markdown-aware hierarchical parsing
    - Semantic boundary detection (headers, paragraphs, lists, code blocks, tables)
    - Context preservation through breadcrumb trails
    - Smart overlap with semantic boundaries
    - Metadata propagation and validation
    """
    
    def __init__(self, 
                 chunk_size: int = 1500,
                 chunk_overlap: int = 200,
                 min_chunk_size: int = 100,
                 max_chunk_size: int = 2500):
        """
        Initialize chunker with configurable parameters.
        
        Args:
            chunk_size: Target size for each chunk (in characters)
            chunk_overlap: Overlap between chunks to maintain context
            min_chunk_size: Minimum acceptable chunk size
            max_chunk_size: Maximum chunk size before forced split
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        
        # Pre-compile regex patterns
        self.header_pattern = re.compile(r'^(#{1,6})\s+(.+)$')
        self.table_pattern = re.compile(r'^\|.*\|')
        self.list_pattern = re.compile(r'^([\*\-\+]|\d+\.)\s+')
    
    def _detect_content_type(self, text: str) -> str:
        """Detect the type of content block"""
        text_stripped = text.strip()
        
        if text_stripped.startswith('```'):
            return 'code_block'
        elif self.table_pattern.match(text_stripped):
            return 'table'
        elif self.list_pattern.match(text_stripped):
            return 'list'
        elif self.header_pattern.match(text_stripped):
            return 'header'
        elif text_stripped.startswith('>'):
            return 'quote'
        else:
            return 'paragraph'
    
    def _split_into_blocks(self, text: str) -> List[Dict]:
        """Split markdown text into semantic blocks"""
        blocks = []
        lines = text.split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i]
            
            # Empty line - skip
            if not line.strip():
                i += 1
                continue
            
            # Detect header
            header_match = self.header_pattern.match(line)
            if header_match:
                level = len(header_match.group(1))
                title = header_match.group(2).strip()
                blocks.append({
                    'type': 'header',
                    'level': level,
                    'title': title,
                    'content': line,
                    'start_line': i
                })
                i += 1
                continue
            
            # Detect code block
            if line.strip().startswith('```'):
                code_lines = [line]
                i += 1
                while i < len(lines):
                    code_lines.append(lines[i])
                    if lines[i].strip().startswith('```'):
                        i += 1
                        break
                    i += 1
                
                blocks.append({
                    'type': 'code_block',
                    'content': '\n'.join(code_lines),
                    'start_line': i - len(code_lines)
                })
                continue
            
            # Detect table
            if self.table_pattern.match(line):
                table_lines = []
                while i < len(lines) and self.table_pattern.match(lines[i]):
                    table_lines.append(lines[i])
                    i += 1
                
                blocks.append({
                    'type': 'table',
                    'content': '\n'.join(table_lines),
                    'start_line': i - len(table_lines)
                })
                continue
            
            # Detect list
            if self.list_pattern.match(line):
                list_lines = []
                indent_level = len(line) - len(line.lstrip())
                
                while i < len(lines):
                    curr_line = lines[i]
                    if not curr_line.strip():
                        i += 1
                        continue
                    
                    curr_indent = len(curr_line) - len(curr_line.lstrip())
                    if self.list_pattern.match(curr_line.lstrip()) or curr_indent > indent_level:
                        list_lines.append(curr_line)
                        i += 1
                    else:
                        break
                
                blocks.append({
                    'type': 'list',
                    'content': '\n'.join(list_lines),
                    'start_line': i - len(list_lines)
                })
                continue
            
            # Regular paragraph
            para_lines = []
            while i < len(lines):
                curr_line = lines[i]
                curr_stripped = curr_line.strip()
                
                # Break on empty line or special markers
                if not curr_stripped:
                    break
                if curr_stripped[0:1] == '#' and self.header_pattern.match(curr_line):
                    break
                if curr_stripped.startswith('```'):
                    break
                if curr_stripped[0:1] == '|' and self.table_pattern.match(curr_line):
                    break
                if self.list_pattern.match(curr_line):
                    break
                    
                para_lines.append(curr_line)
                i += 1
            
            if para_lines:
                blocks.append({
                    'type': 'paragraph',
                    'content': '\n'.join(para_lines),
                    'start_line': i - len(para_lines)
                })
        
        return blocks
    
    def _build_hierarchy(self, blocks: List[Dict]) -> List[Dict]:
        """Build hierarchical structure tracking parent headers"""
        header_stack = []  # [(level, title, header_text), ...]
        
        for block in blocks:
            if block['type'] == 'header':
                level = block['level']
                title = block['title']
                
                # Pop headers of same or greater level
                while header_stack and header_stack[-1][0] >= level:
                    header_stack.pop()
                
                # Add current header to stack
                header_stack.append((level, title, block['content']))
                block['parent_headers'] = [h[1] for h in header_stack[:-1]]
                block['breadcrumb'] = ' > '.join([h[1] for h in header_stack])
            else:
                # Non-header blocks inherit current hierarchy
                block['parent_headers'] = [h[1] for h in header_stack]
                block['breadcrumb'] = ' > '.join([h[1] for h in header_stack]) if header_stack else ''
        
        return blocks
    
    def _create_chunks_from_blocks(self, blocks: List[Dict], page_metadata: Dict) -> List[Dict]:
        """Combine blocks into chunks respecting semantic boundaries"""
        chunks = []
        current_chunk_blocks = []
        current_size = 0
        
        for i, block in enumerate(blocks):
            block_content = block['content']
            block_size = len(block_content)
            context_prefix = f"**{block['breadcrumb']}**\n\n" if block['breadcrumb'] else ""
            
            # Special handling for large blocks
            if block['type'] in ['code_block', 'table'] and block_size > self.chunk_size:
                # Save current chunk if exists
                if current_chunk_blocks:
                    chunks.append(self._finalize_chunk(current_chunk_blocks, page_metadata, len(chunks)))
                    current_chunk_blocks = []
                    current_size = 0
                
                # Split large block if necessary
                if block_size > self.max_chunk_size:
                    if block['type'] == 'table':
                        # Use smart table splitting that preserves headers
                        table_chunks = self._split_table_preserving_headers(
                            block_content, context_prefix, block, page_metadata, len(chunks)
                        )
                        chunks.extend(table_chunks)
                    else:
                        # Original line-based splitting for code blocks
                        lines = block_content.split('\n')
                        temp_lines = []
                        temp_size = 0
                        
                        for line in lines:
                            if temp_size + len(line) > self.chunk_size and temp_lines:
                                chunk_content = context_prefix + '\n'.join(temp_lines)
                                chunks.append(self._create_chunk_dict(chunk_content, block, page_metadata, len(chunks)))
                                temp_lines = []
                                temp_size = 0
                            
                            temp_lines.append(line)
                            temp_size += len(line)
                        
                        if temp_lines:
                            chunk_content = context_prefix + '\n'.join(temp_lines)
                            chunks.append(self._create_chunk_dict(chunk_content, block, page_metadata, len(chunks)))
                else:
                    chunk_content = context_prefix + block_content
                    chunks.append(self._create_chunk_dict(chunk_content, block, page_metadata, len(chunks)))
                
                continue
            
            # Check if adding block exceeds chunk size
            projected_size = current_size + block_size + len(context_prefix) + 10
            
            if projected_size > self.chunk_size and current_chunk_blocks:
                # Save current chunk
                chunks.append(self._finalize_chunk(current_chunk_blocks, page_metadata, len(chunks)))
                
                # Start new chunk with overlap
                overlap_blocks = self._get_overlap_blocks(current_chunk_blocks)
                current_chunk_blocks = overlap_blocks
                current_size = sum(len(b['content']) for b in overlap_blocks)
            
            # Add block to current chunk
            block_with_context = block.copy()
            block_with_context['context_prefix'] = context_prefix
            current_chunk_blocks.append(block_with_context)
            current_size += block_size + len(context_prefix)
        
        # Don't forget last chunk
        if current_chunk_blocks:
            chunks.append(self._finalize_chunk(current_chunk_blocks, page_metadata, len(chunks)))
        
        return chunks
    
    def _split_table_preserving_headers(self, table_content: str, context_prefix: str, 
                                         block: Dict, page_metadata: Dict, 
                                         start_chunk_index: int) -> List[Dict]:
        """
        Split large tables while preserving:
        1. Table title/context
        2. Column headers in each chunk
        3. Row-by-row details section
        """
        chunks = []
        lines = table_content.split('\n')
        
        # Separate table parts
        table_title = ""
        header_line = ""
        separator_line = ""
        data_lines = []
        row_by_row_lines = []
        in_row_by_row = False
        
        for line in lines:
            if line.startswith('**Table:'):
                table_title = line
            elif line.startswith('--- Row-by-Row Details ---'):
                in_row_by_row = True
            elif in_row_by_row:
                row_by_row_lines.append(line)
            elif line.startswith('|') and '---' in line:
                separator_line = line
            elif line.startswith('|') and not header_line:
                header_line = line
            elif line.startswith('|'):
                data_lines.append(line)
            elif line.strip():
                # Could be title or context
                if not table_title and line.startswith('**'):
                    table_title = line
        
        # Build header section that will be repeated in each chunk
        header_section = ""
        if table_title:
            header_section = table_title + "\n"
        if header_line:
            header_section += header_line + "\n"
        if separator_line:
            header_section += separator_line + "\n"
        
        header_size = len(context_prefix) + len(header_section)
        available_size = self.chunk_size - header_size - 100  # Buffer
        
        # Chunk the data rows
        if data_lines:
            current_rows = []
            current_size = 0
            
            for row in data_lines:
                row_size = len(row) + 1  # +1 for newline
                if current_size + row_size > available_size and current_rows:
                    # Create chunk with header + current rows
                    chunk_content = context_prefix + header_section + '\n'.join(current_rows)
                    chunks.append(self._create_chunk_dict(
                        chunk_content, block, page_metadata, start_chunk_index + len(chunks)
                    ))
                    current_rows = []
                    current_size = 0
                
                current_rows.append(row)
                current_size += row_size
            
            # Add remaining rows
            if current_rows:
                chunk_content = context_prefix + header_section + '\n'.join(current_rows)
                chunks.append(self._create_chunk_dict(
                    chunk_content, block, page_metadata, start_chunk_index + len(chunks)
                ))
        
        # Chunk the row-by-row section separately (very important for retrieval)
        if row_by_row_lines:
            row_by_row_header = ""
            if table_title:
                row_by_row_header = table_title.replace('**Table:', '**Table Row Details:') + "\n"
            row_by_row_header += "--- Row-by-Row Details ---\n"
            
            header_size = len(context_prefix) + len(row_by_row_header)
            available_size = self.chunk_size - header_size - 50
            
            current_rows = []
            current_size = 0
            
            for row in row_by_row_lines:
                if not row.strip():
                    continue
                row_size = len(row) + 1
                if current_size + row_size > available_size and current_rows:
                    chunk_content = context_prefix + row_by_row_header + '\n'.join(current_rows)
                    chunks.append(self._create_chunk_dict(
                        chunk_content, block, page_metadata, start_chunk_index + len(chunks)
                    ))
                    current_rows = []
                    current_size = 0
                
                current_rows.append(row)
                current_size += row_size
            
            if current_rows:
                chunk_content = context_prefix + row_by_row_header + '\n'.join(current_rows)
                chunks.append(self._create_chunk_dict(
                    chunk_content, block, page_metadata, start_chunk_index + len(chunks)
                ))
        
        # If no chunks were created (small table), create one chunk with everything
        if not chunks:
            chunk_content = context_prefix + table_content
            chunks.append(self._create_chunk_dict(
                chunk_content, block, page_metadata, start_chunk_index
            ))
        
        return chunks
    
    def _get_overlap_blocks(self, blocks: List[Dict]) -> List[Dict]:
        """Get blocks for overlap"""
        if not blocks:
            return []
        
        overlap_blocks = []
        overlap_size = 0
        
        for block in reversed(blocks):
            if overlap_size >= self.chunk_overlap:
                break
            overlap_blocks.insert(0, block)
            overlap_size += len(block['content'])
        
        return overlap_blocks
    
    def _finalize_chunk(self, blocks: List[Dict], page_metadata: Dict, chunk_index: int) -> Dict:
        """Create final chunk dictionary from blocks"""
        chunk_parts = []
        breadcrumb = ""
        
        for block in blocks:
            context = block.get('context_prefix', '')
            if context and context not in chunk_parts:
                breadcrumb = block.get('breadcrumb', '')
            chunk_parts.append(block['content'])
        
        chunk_text = '\n\n'.join(chunk_parts)
        
        # Add breadcrumb at the top if available
        if breadcrumb:
            chunk_text = f"**Context:** {breadcrumb}\n\n{chunk_text}"
        
        return self._create_chunk_dict(chunk_text, blocks[0] if blocks else {}, page_metadata, chunk_index)
    
    def _create_chunk_dict(self, content: str, block: Dict, page_metadata: Dict, chunk_index: int) -> Dict:
        """Create standardized chunk dictionary with enhanced table metadata"""
        
        # Extract table-specific metadata for better retrieval
        table_title = ""
        table_columns = ""
        has_table_content = False
        
        if block.get('type') == 'table' or '| ' in content:
            has_table_content = True
            # Extract table title
            import re
            title_match = re.search(r'\*\*Table:\s*([^*]+)\*\*', content)
            if title_match:
                table_title = title_match.group(1).strip()
            
            # Extract column headers from markdown table (first | row that's NOT a separator)
            # Match lines like: | Col1 | Col2 | Col3 | but NOT | --- | --- | --- |
            lines = content.split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('|') and line.endswith('|') and '---' not in line:
                    # This is likely the header row
                    columns = [col.strip() for col in line.split('|') if col.strip()]
                    if columns:
                        table_columns = ', '.join(columns)
                        break
        
        return {
            'id': f"{page_metadata.get('page_id', 'unknown')}_chunk_{chunk_index}",
            'text': content,
            'metadata': {
                # Page-level metadata
                'page_id': str(page_metadata.get('page_id', '')),
                'title': page_metadata.get('title', ''),
                'space_key': page_metadata.get('space_key', ''),
                'space_name': page_metadata.get('space_name', ''),
                'url': page_metadata.get('url', ''),
                'version': page_metadata.get('version', 1),
                
                # Chunk-level metadata
                'chunk_index': chunk_index,
                'chunk_size': len(content),
                'content_type': block.get('type', 'mixed'),
                'breadcrumbs': block.get('breadcrumb', ''),
                'parent_headers': ', '.join(block.get('parent_headers', [])) if isinstance(block.get('parent_headers', []), list) else str(block.get('parent_headers', '')),
                
                # Table-specific metadata (NEW - for better table retrieval)
                'has_table': has_table_content,
                'table_title': table_title,
                'table_columns': table_columns,
                
                # Link metadata for better retrieval
                'jira_links': page_metadata.get('metadata', {}).get('jira_links', ''),
                'external_links': page_metadata.get('metadata', {}).get('external_links', ''),
                'internal_links': page_metadata.get('metadata', {}).get('internal_links', ''),
                
                # Additional context
                'source': 'confluence',
                'ingestion_strategy': 'smart_semantic_chunking'
            }
        }
    
    def chunk_page(self, page: Dict) -> List[Dict]:
        """Chunk a single page with full metadata preservation"""
        if not page.get('content'):
            return []
        
        # Prepare full text with title
        title = page.get('title', 'Untitled')
        content = page.get('content', '')
        full_text = f"# {title}\n\n{content}"
        
        # Parse into semantic blocks
        blocks = self._split_into_blocks(full_text)
        
        # Build hierarchy
        blocks = self._build_hierarchy(blocks)
        
        # Create chunks from blocks
        chunks = self._create_chunks_from_blocks(blocks, page)
        
        # Update total_chunks in metadata
        for chunk in chunks:
            chunk['metadata']['total_chunks'] = len(chunks)
        
        return chunks
    
    def chunk_documents(self, pages: List[Dict]) -> Tuple[List[Dict], Dict]:
        """Chunk multiple documents with statistics"""
        all_chunks = []
        stats = {
            'total_pages': len(pages),
            'total_chunks': 0,
            'avg_chunks_per_page': 0,
            'avg_chunk_size': 0,
            'min_chunk_size': float('inf'),
            'max_chunk_size': 0,
            'content_types': {},
            'pages_with_errors': []
        }
        
        for idx, page in enumerate(pages):
            try:
                page_chunks = self.chunk_page(page)
                all_chunks.extend(page_chunks)
                
                # Update statistics
                for chunk in page_chunks:
                    size = chunk['metadata']['chunk_size']
                    stats['min_chunk_size'] = min(stats['min_chunk_size'], size)
                    stats['max_chunk_size'] = max(stats['max_chunk_size'], size)
                    
                    content_type = chunk['metadata']['content_type']
                    stats['content_types'][content_type] = stats['content_types'].get(content_type, 0) + 1
                
            except Exception as e:
                stats['pages_with_errors'].append({
                    'page_id': page.get('page_id', 'unknown'),
                    'title': page.get('title', 'unknown'),
                    'error': str(e)
                })
        
        # Calculate averages
        stats['total_chunks'] = len(all_chunks)
        if stats['total_pages'] > 0:
            stats['avg_chunks_per_page'] = stats['total_chunks'] / stats['total_pages']
        if stats['total_chunks'] > 0:
            stats['avg_chunk_size'] = sum(c['metadata']['chunk_size'] for c in all_chunks) / stats['total_chunks']
        
        # Fix infinity
        if stats['min_chunk_size'] == float('inf'):
            stats['min_chunk_size'] = 0
        
        return all_chunks, stats
