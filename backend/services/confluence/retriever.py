"""
Enhanced Confluence Retriever
Preserves exact logic from notebook for multi-strategy retrieval and reranking
Uses a JSONL file-backed vector store
"""

import numpy as np
from typing import List, Dict, Tuple, Optional, Any
from sentence_transformers import CrossEncoder, SentenceTransformer
from rank_bm25 import BM25Okapi
import re
import json
import os
from datetime import datetime
import hashlib

# Optional caching support
try:
    from cachetools import LRUCache, TTLCache
    CACHING_AVAILABLE = True
except ImportError:
    CACHING_AVAILABLE = False


def get_metadata_field(metadata: dict, field_name: str, default: str = 'Unknown') -> str:
    """Helper function to get metadata field, handling both old and new naming conventions."""
    if field_name == 'title':
        return metadata.get('title', metadata.get('page_title', default))
    elif field_name == 'content_type':
        return metadata.get('content_type', metadata.get('section_type', default))
    else:
        return metadata.get(field_name, default)


def sanitize_jira_content(content: str) -> str:
    """Clean up malformed JIRA macro outputs from retrieved content."""
    if not content:
        return content
    
    jira_garbage_pattern = r'([A-Z]{2,10}-\d+)([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:System Jira|Jira)?'
    
    def fix_jira_garbage(match):
        ticket_id = match.group(1)
        jira_url = f"https://welldoc.atlassian.net/browse/{ticket_id}"
        return f"[{ticket_id}]({jira_url})"
    
    content = re.sub(jira_garbage_pattern, fix_jira_garbage, content, flags=re.IGNORECASE)
    return content


class EnhancedConfluenceRetriever:
    """
    Enhanced retrieval system for Confluence pages with multiple strategies
    and reranking for better accuracy. Uses JSONL vector storage.
    """
    
    def __init__(self, 
                 vector_store,
                 collection_name: str,
                 cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 use_bm25: bool = True,
                 use_hybrid_search: bool = True,
                 enable_cache: bool = False,
                 query_cache_size: int = 1000,
                 query_cache_ttl: int = 1800,
                 chroma_collection=None):
        
        self.vector_store = vector_store
        self.qdrant_client = vector_store  # Compatibility name for older call sites.
        self.collection_name = collection_name
        
        # Initialize embedding model
        model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
        self.embedding_model = SentenceTransformer(model_name)
        
        # Initialize reranking models
        try:
            self.cross_encoder = CrossEncoder(cross_encoder_model)
            self.use_cross_encoder = True
        except Exception:
            print(f"Warning: Could not load cross-encoder model {cross_encoder_model}")
            self.use_cross_encoder = False
            
        self.use_bm25 = use_bm25
        self.use_hybrid_search = use_hybrid_search
        
        # Reranking weights
        self.weights = {
            'vector_similarity': 0.35,
            'bm25_score': 0.30,
            'cross_encoder_score': 0.35,
            'recency_bonus': 0.08,
            'hierarchy_bonus': 0.07
        }
        
        self.enable_context_enhancement = os.getenv("ENABLE_CONTEXT_ENHANCEMENT", "false").lower() == "true"
        
        # Optional caching
        self.enable_cache = enable_cache and CACHING_AVAILABLE
        self.query_cache = None
        self.cache_hits = 0
        self.cache_misses = 0
        
        if self.enable_cache:
            try:
                self.query_cache = TTLCache(maxsize=query_cache_size, ttl=query_cache_ttl)
                print(f"✓ Query cache enabled: size={query_cache_size}, ttl={query_cache_ttl}s")
            except Exception as e:
                print(f"Warning: Could not initialize cache: {e}")
                self.enable_cache = False

    def _get_query_strategy(self, query: str) -> dict:
        """Get retrieval strategy tailored to specific fixed query contents"""
        query_lower = query.lower()
        
        # Enumeration Strategy (definitions, acronyms)
        if any(x in query_lower for x in ["definition", "acronym", "term", "abbreviation"]):
            return {
                "query_type": "enumeration",
                "use_exact": True,
                "n_initial": 200,
                "max_per_page": 1,
                "supplementary_keyword": True,
                "negation_title_scroll": False
            }
            
        # Link Collection Strategy (references, citations)
        if any(x in query_lower for x in ["reference", "citation", "external link", "internal link"]):
            return {
                "query_type": "link_collection",
                "use_exact": True,
                "n_initial": 250,
                "max_per_page": 2,
                "supplementary_keyword": True,
                "negation_title_scroll": False
            }
            
        # Negation Strategy (out of scope, non-goals)
        if any(x in query_lower for x in ["out of scope", "out-of-scope", "non-goal", "exclusion", "exclude"]):
            return {
                "query_type": "negation",
                "use_exact": True,
                "n_initial": 100,
                "max_per_page": 3,
                "supplementary_keyword": True,
                "negation_title_scroll": True
            }
            
        # Changelog/History Strategy
        if any(x in query_lower for x in ["changelog", "history", "version", "release"]):
            return {
                "query_type": "changelog",
                "use_exact": True,
                "n_initial": 150,
                "max_per_page": 3,
                "supplementary_keyword": True,
                "negation_title_scroll": False
            }
            
        # Temporal Strategy
        if any(x in query_lower for x in ["temporal", "schedule", "timeline", "date", "time"]):
            return {
                "query_type": "temporal",
                "use_exact": True,
                "n_initial": 150,
                "max_per_page": 2,
                "supplementary_keyword": True,
                "negation_title_scroll": False
            }
            
        # Default Standard Strategy
        return {
            "query_type": "standard",
            "use_exact": False,
            "n_initial": 20,
            "max_per_page": 3,
            "supplementary_keyword": False,
            "negation_title_scroll": False
        }

    def _expand_query(self, query: str) -> str:
        """Basic query expansion to improve vector search recall"""
        return query

    def retrieve_initial(self, 
                         query: str, 
                         n_initial: int = 20,
                         filters: Dict = None,
                         product: str = None,
                         use_exact: bool = False) -> List[Dict]:
        """Initial retrieval using vector similarity from JSONL embeddings."""
        expanded_query = self._expand_query(query)
        query_vector = self.embedding_model.encode(expanded_query, normalize_embeddings=True).tolist()

        return self.vector_store.search(
            query_vector=query_vector,
            limit=n_initial,
            product=product,
            filters=filters,
        )

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization for BM25"""
        tokens = re.findall(r'\b\w+\b', text.lower())
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        return [token for token in tokens if token not in stop_words and len(token) > 1]

    def apply_bm25_reranking(self, query: str, results: List[Dict]) -> List[Dict]:
        """Apply BM25 scoring to rerank results"""
        if not self.use_bm25 or not results:
            return results
        
        query_tokens = self._tokenize(query)
        docs = []
        doc_to_result_map = []
        
        for result in results:
            title = get_metadata_field(result['metadata'], 'title', '')
            breadcrumbs_raw = result['metadata'].get('breadcrumbs', '')
            if isinstance(breadcrumbs_raw, str):
                breadcrumbs = breadcrumbs_raw
            elif isinstance(breadcrumbs_raw, list):
                breadcrumbs = ' '.join(breadcrumbs_raw)
            else:
                breadcrumbs = ''
            
            table_title = result['metadata'].get('table_title', '')
            table_columns = result['metadata'].get('table_columns', '')
            parent_headers = result['metadata'].get('parent_headers', '')
            
            full_text = f"{title} {breadcrumbs} {table_title} {table_columns} {parent_headers} {result['content']}"
            tokens = self._tokenize(full_text)
            if tokens:
                docs.append(tokens)
                doc_to_result_map.append(result)
        
        if not docs:
            return results
        
        bm25 = BM25Okapi(docs)
        bm25_scores = bm25.get_scores(query_tokens)
        
        if len(bm25_scores) > 0:
            max_score = max(bm25_scores)
            if max_score > 0:
                bm25_scores = [score / max_score for score in bm25_scores]
        
        for i, result in enumerate(doc_to_result_map):
            result['bm25_score'] = bm25_scores[i] if i < len(bm25_scores) else 0.0
        
        return results

    def apply_cross_encoder_reranking(self, query: str, results: List[Dict]) -> List[Dict]:
        """Apply cross-encoder for more accurate reranking"""
        if not self.use_cross_encoder or not results:
            return results
        
        try:
            pairs = []
            for result in results:
                title = get_metadata_field(result['metadata'], 'title', '')
                content = result['content'][:512]
                pairs.append([query, f"{title}: {content}"])
            
            ce_scores = self.cross_encoder.predict(pairs)
            
            if len(ce_scores) > 0:
                max_score = max(ce_scores)
                if max_score > 0:
                    ce_scores = [score / max_score for score in ce_scores]
            
            for i, result in enumerate(results):
                if i < len(ce_scores):
                    result['cross_encoder_score'] = float(ce_scores[i])
                else:
                    result['cross_encoder_score'] = 0.0
                    
        except Exception as e:
            print(f"Warning: Cross-encoder reranking failed: {e}")
            for result in results:
                result['cross_encoder_score'] = 0.0
        
        return results

    def apply_metadata_boosting(self, results: List[Dict], query: str = '') -> List[Dict]:
        """Apply boosting based on metadata and query context"""
        ticket_pattern = r'\b([A-Z]{2,10}-\d+)\b'
        query_ticket_ids = set(re.findall(ticket_pattern, query, re.IGNORECASE))
        query_lower = query.lower()
        
        table_column_keywords = {
            'requirements': ['requirement', 'requirements', 'scope', 'spec'],
            'jira': ['jira', 'user story', 'story', 'ticket', 'issue', 'epic', 'jira epic', 'system story'],
            'tdc': ['tdc', 'tdc/derx', 'tdc/ntn', 'tdc/wm'],
            'wm': ['wm', 'wm standalone', 'standalone'],
            'accordant': ['accordant'],
            'derx': ['derx', 'de rx'],
            'comments': ['comment', 'comments', 'notes', 'remarks', 'description'],
            'approvals': ['approval', 'approvals', 'approved', 'sign-off', 'yes/no'],
            'owner': ['owner', 'product owner', 'document owner', 'po'],
            'qa': ['qa', 'quality assurance', 'testing', 'qe'],
            'developer': ['developer', 'developers', 'dev', 'engineering'],
            'designer': ['designer', 'designers', 'design', 'ux', 'human factors'],
            'pm': ['project manager', 'pm', 'program manager'],
            'release': ['release', 'target release', 'version'],
            'regulatory': ['regulatory', 'regulatory team', 'compliance'],
            'clinical': ['clinical', 'clinical team', 'medical'],
            'item': ['item', 'items', 'scope item', 'line item'],
            'confluence': ['confluence', 'confluence link', 'page'],
            'epic': ['epic', 'jira epic', 'epics', 'feature']
        }
        
        queried_columns = set()
        for col_type, keywords in table_column_keywords.items():
            if any(kw in query_lower for kw in keywords):
                queried_columns.add(col_type)
        
        version_pattern = r'\b(\d+\.\d+(?:\.\d+)?)\b'
        version_matches = re.findall(version_pattern, query)
        
        named_pattern = r'\b([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*)\s+(\d+\.\d+(?:\.\d+)?)\b'
        named_matches = re.findall(named_pattern, query)
        
        for result in results:
            metadata = result['metadata']
            content = result['content']
            boost = 0.0
            
            if 'version' in metadata:
                try:
                    version_num = int(metadata['version'])
                    if version_num > 10:
                        boost += self.weights['recency_bonus']
                except Exception:
                    pass
            
            level = metadata.get('level', 1)
            if level <= 2:
                boost += self.weights['hierarchy_bonus']
            
            content_type = get_metadata_field(metadata, 'content_type', '')
            if content_type in ['table', 'code_block']:
                boost += 0.02
            
            has_table = metadata.get('has_table', False) or content_type == 'table' or '| ' in content
            table_title = metadata.get('table_title', '')
            table_columns = metadata.get('table_columns', '').lower()
            
            if has_table and queried_columns:
                for col_type in queried_columns:
                    col_keywords = table_column_keywords[col_type]
                    if any(kw in table_columns for kw in col_keywords):
                        boost += 0.15
                        break
                    if any(kw in content.lower() for kw in col_keywords):
                        boost += 0.10
                        break
            
            if version_matches:
                for version in version_matches:
                    if version in content or version in table_title:
                        boost += 0.15
                        break
                    breadcrumbs = metadata.get('breadcrumbs', '').lower()
                    parent_headers = metadata.get('parent_headers', '').lower()
                    if version in breadcrumbs or version in parent_headers:
                        boost += 0.12
                        break
            
            if named_matches and table_title:
                for name_part, version_part in named_matches:
                    search_term = f"{name_part.strip()} {version_part}".lower()
                    if search_term in table_title.lower():
                        boost += 0.25
                        break
                    breadcrumbs = metadata.get('breadcrumbs', '').lower()
                    parent_headers = metadata.get('parent_headers', '').lower()
                    if search_term in breadcrumbs or search_term in parent_headers:
                        boost += 0.20
                        break
            
            page_title = get_metadata_field(metadata, 'title', '').lower()
            if named_matches:
                for name_part, version_part in named_matches:
                    search_term = f"{name_part.strip()} {version_part}".lower()
                    if search_term in page_title:
                        boost += 0.30
                        break
            
            if version_matches:
                query_version = version_matches[0]
                query_version_parts = query_version.split('.')
                
                title_version_pattern = r'\b(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)\b'
                title_versions = re.findall(title_version_pattern, page_title)
                
                version_matched = False
                version_mismatched = False
                
                for title_ver in title_versions:
                    if title_ver == query_version:
                        boost += 0.40
                        version_matched = True
                        break
                    else:
                        title_ver_parts = title_ver.split('.')
                        if len(query_version_parts) >= 2 and len(title_ver_parts) >= 2:
                            if query_version_parts[0] == title_ver_parts[0] and query_version_parts[1] != title_ver_parts[1]:
                                version_mismatched = True
                
                if not version_matched:
                    if version_mismatched:
                        boost -= 0.30
                    else:
                        content_lower = content.lower()
                        if f" {query_version}" in content_lower or f"/{query_version}" in content_lower:
                            boost += 0.20
            
            if 'Row-by-Row Details' in content or 'Row ' in content:
                if queried_columns:
                    boost += 0.12
            
            if query_ticket_ids:
                content_ticket_ids = set(re.findall(ticket_pattern, content, re.IGNORECASE))
                title_ticket_ids = set(re.findall(ticket_pattern, page_title, re.IGNORECASE))
                jira_links_str = metadata.get('jira_links', '')
                metadata_ticket_ids = set(re.findall(ticket_pattern, jira_links_str, re.IGNORECASE))
                
                matching_ids = query_ticket_ids & (content_ticket_ids | title_ticket_ids | metadata_ticket_ids)
                if matching_ids:
                    boost += 0.20
                    
            jira_keywords = ['jira', 'ticket', 'issue', 'story', 'bug', 'task']
            if any(keyword in query_lower for keyword in jira_keywords):
                jira_links_str = metadata.get('jira_links', '')
                if jira_links_str and len(jira_links_str) > 0:
                    boost += 0.10
            
            # Negation query boosting
            is_negation_query = any(x in query_lower for x in ["out of scope", "out-of-scope", "non-goal", "exclusion", "exclude"])
            if is_negation_query:
                negation_markers = [
                    'descoped', 'de-scoped', 'out of scope', 'out-of-scope',
                    'excluded', 'exclusion', 'archived', 'deprecated',
                    'removed', 'dropped', 'deferred', 'backlog',
                    'not included', 'not in scope', 'not in use', 'not supported'
                ]
                content_lower = content.lower()
                if any(marker in page_title for marker in negation_markers):
                    boost += 0.35
                elif any(marker in content_lower for marker in negation_markers):
                    boost += 0.20
            
            # Product-line filter boosting
            is_prod_filter = any(x in query_lower for x in ["product line", "product-line", "multitenancy", "tenant"])
            if is_prod_filter:
                if has_table:
                    boost += 0.15
                    product_line_cols = ['tdc', 'derx', 'de rx', 'wm standalone', 'wm', 'accordant', 'ntn', 'health optimizer']
                    if any(plc in table_columns for plc in product_line_cols):
                        boost += 0.20
                    elif any(plc in content.lower() for plc in product_line_cols):
                        boost += 0.10
                if 'Row-by-Row Details' in content or 'Row ' in content:
                    boost += 0.10
            
            # Temporal boosting
            is_temporal = any(x in query_lower for x in ["temporal", "schedule", "timeline", "date", "time"])
            if is_temporal:
                title_version_pattern = r'\b(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)\b'
                title_versions = re.findall(title_version_pattern, page_title)
                if title_versions:
                    try:
                        ver_tuple = tuple(int(x) for x in title_versions[0].split('.'))
                        recency_value = ver_tuple[0] * 10 + (ver_tuple[1] if len(ver_tuple) > 1 else 0)
                        recency_boost = min(recency_value / 50.0 * 0.30, 0.30)
                        boost += recency_boost
                    except Exception:
                        pass
                    boost += 0.10
            
            result['metadata_boost'] = boost
        
        return results

    def calculate_version_relevance_boost(self, query: str, document_content: str, metadata: Dict) -> float:
        """Calculate version-specific relevance boost/penalty for version queries."""
        version_pattern = r'\b(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)\b'
        query_versions = re.findall(version_pattern, query, re.IGNORECASE)
        
        if not query_versions:
            return 0.0
        
        query_version = query_versions[0]
        doc_versions = re.findall(version_pattern, document_content, re.IGNORECASE)
        title = metadata.get('title', '')
        title_versions = re.findall(version_pattern, title, re.IGNORECASE)
        url_versions = re.findall(version_pattern, metadata.get('url', ''), re.IGNORECASE)
        
        all_doc_versions = set(doc_versions + title_versions + url_versions)
        if not all_doc_versions:
            return 0.0
        
        query_parts = query_version.split('.')
        
        if query_version in all_doc_versions:
            return 0.25
        
        if query_version in title:
            return 0.30
        
        has_different_version_in_title = False
        for title_ver in title_versions:
            if title_ver != query_version:
                title_parts = title_ver.split('.')
                if len(query_parts) >= 2 and len(title_parts) >= 2:
                    if query_parts[0] == title_parts[0] and query_parts[1] != title_parts[1]:
                        has_different_version_in_title = True
                        break
        
        if has_different_version_in_title:
            return -0.35
        
        has_different_version_in_content = False
        for doc_ver in doc_versions[:5]:
            if doc_ver != query_version:
                doc_parts = doc_ver.split('.')
                if len(query_parts) >= 2 and len(doc_parts) >= 2:
                    if query_parts[0] == doc_parts[0] and query_parts[1] != doc_parts[1]:
                        has_different_version_in_content = True
                        break
        
        if has_different_version_in_content:
            return -0.15
        
        max_boost = 0.0
        for doc_version in all_doc_versions:
            doc_parts = doc_version.split('.')
            min_length = min(len(query_parts), len(doc_parts))
            if query_parts[:min_length] == doc_parts[:min_length]:
                if len(query_parts) > len(doc_parts):
                    max_boost = max(max_boost, 0.05)
                elif len(doc_parts) > len(query_parts):
                    max_boost = max(max_boost, 0.03)
        
        return max_boost

    def calculate_combined_scores(self, results: List[Dict], query: str = "") -> List[Dict]:
        """Calculate combined scores from multiple ranking signals including version relevance"""
        for result in results:
            vector_score = result.get('vector_similarity', 0.0)
            bm25_score = result.get('bm25_score', vector_score)
            ce_score = result.get('cross_encoder_score', vector_score)
            metadata_boost = result.get('metadata_boost', 0.0)
            
            version_boost = 0.0
            if query:
                version_boost = self.calculate_version_relevance_boost(
                    query, 
                    result.get('content', ''), 
                    result.get('metadata', {})
                )
            
            combined = (
                vector_score * self.weights['vector_similarity'] +
                bm25_score * self.weights['bm25_score'] +
                ce_score * self.weights['cross_encoder_score'] +
                metadata_boost +
                version_boost
            )
            
            result['version_boost'] = version_boost
            result['combined_score'] = combined
        
        return results

    def remove_duplicates(self, results: List[Dict], threshold: float = 0.9) -> List[Dict]:
        """Remove near-duplicate results"""
        if not results:
            return results
        
        results.sort(key=lambda x: x['combined_score'], reverse=True)
        unique_results = []
        seen_content_hashes = set()
        
        for result in results:
            content = result['content'][:200]
            content_hash = hash(content)
            
            if content_hash not in seen_content_hashes:
                seen_content_hashes.add(content_hash)
                unique_results.append(result)
        
        return unique_results

    def _extract_content_keywords(self, query: str) -> List[str]:
        """Extract key content search terms from a query for keyword-based search."""
        query_lower = query.lower().strip().rstrip('?.')
        
        object_patterns = [
            r'(?:contains?|have|has|having|includes?|including|with)\s+(?:any\s+|some\s+)?(?:requirements?\s+for\s+(?:any\s+)?)?(.+?)(?:\?|$)',
            r'(?:related\s+to|about|regarding|mentioning|involving)\s+(.+?)(?:\?|$)',
            r'(?:where|in\s+which)\s+(?:there\s+(?:is|are)\s+)?(.+?)(?:\?|$)',
        ]
        
        for pattern in object_patterns:
            match = re.search(pattern, query_lower)
            if match:
                raw_terms = match.group(1).strip().rstrip('?.')
                if len(raw_terms) < 3:
                    continue
                terms = [raw_terms]
                words = [w for w in raw_terms.split() if len(w) > 3]
                terms.extend(words)
                word_list = raw_terms.split()
                for i in range(len(word_list) - 1):
                    pair = f"{word_list[i]} {word_list[i+1]}"
                    if len(pair) > 5:
                        terms.append(pair)
                return list(set(terms))
        
        stop_words = {'which', 'what', 'list', 'name', 'find', 'identify', 'show', 'give',
                      'releases', 'release', 'versions', 'version', 'pages', 'page',
                      'that', 'contain', 'contains', 'containing', 'have', 'has', 'having',
                      'include', 'includes', 'including', 'with', 'where',
                      'in', 'of', 'for', 'from', 'any', 'all', 'the', 'a', 'an',
                      'me', 'please', 'could', 'can', 'you', 'do', 'does', 'are', 'is'}
        words = re.findall(r'\b\w+\b', query_lower)
        content_words = [w for w in words if w not in stop_words and len(w) > 2]
        return content_words if content_words else [query_lower]

    def _supplementary_keyword_scroll(self, query: str, product: str = None, max_results: int = 200) -> List[Dict]:
        """Scroll through documents and find ones containing key terms from the query."""
        terms = self._extract_content_keywords(query)
        if not terms:
            return []

        results = []
        seen_ids = set()

        try:
            for record in self.vector_store.records():
                if len(results) >= max_results:
                    break
                metadata = record.get("metadata") or {}
                if product and metadata.get("product") != product:
                    continue
                point_id = str(record.get("id"))
                if point_id in seen_ids:
                    continue

                content = (record.get("content") or record.get("text") or "").lower()
                title = get_metadata_field(metadata, 'title', '').lower()

                title_hit = any(term in title for term in terms)
                content_hit = any(term in content for term in terms)

                if title_hit or content_hit:
                    if title_hit and content_hit:
                        kw_score = 0.75
                    elif title_hit:
                        kw_score = 0.65
                    else:
                        kw_score = 0.45

                    seen_ids.add(point_id)
                    results.append({
                        'id': point_id,
                        'content': record.get("content", ""),
                        'metadata': metadata,
                        'vector_distance': 1.0 - kw_score,
                        'vector_similarity': kw_score,
                        'combined_score': kw_score,
                        'keyword_match': True
                    })

        except Exception as e:
            print(f"    Warning: Keyword scroll search failed: {e}")

        return results

    def ensure_page_diversity(self, results: List[Dict], max_per_page: int = 3) -> List[Dict]:
        """Ensure results represent as many different pages as possible."""
        if not results:
            return results
        
        page_groups = {}
        for r in results:
            title = get_metadata_field(r.get('metadata', {}), 'title', 'Unknown')
            if title not in page_groups:
                page_groups[title] = []
            page_groups[title].append(r)
        
        for title in page_groups:
            page_groups[title].sort(key=lambda x: x.get('combined_score', 0), reverse=True)
        
        diverse_results = []
        for title in page_groups:
            diverse_results.append(page_groups[title][0])
        
        diverse_results.sort(key=lambda x: x.get('combined_score', 0), reverse=True)
        
        for title in page_groups:
            for chunk in page_groups[title][1:max_per_page]:
                if chunk not in diverse_results:
                    diverse_results.append(chunk)
        
        diverse_results.sort(key=lambda x: x.get('combined_score', 0), reverse=True)
        return diverse_results

    def _generate_query_variants(self, query: str) -> List[str]:
        """Generate alternate phrasings of a query to improve retrieval recall."""
        query_stripped = query.strip().rstrip('?.')
        query_lower = query_stripped.lower()
        variants = []
        
        noun_phrase = query_stripped
        qword_patterns = [
            r'^how\s+(?:do|does|did|can|could|would|will|should)\s+(?:i|you|we|one)\s+',
            r'^(?:what|which|where|who|when|how)\s+(?:are|is|was|were|do|does|did|can|could|would|will|should|has|have|had)\s+(?:the\s+)?',
            r'^(?:what|which|where|who|when|how)\s+',
            r'^(?:can|could|would|will|should|do|does|did)\s+(?:you\s+)?(?:tell\s+me\s+(?:about\s+)?|list\s+|show\s+(?:me\s+)?|find\s+|give\s+(?:me\s+)?|provide\s+)?(?:the\s+|all\s+(?:the\s+)?)?',
            r'^(?:list|name|find|identify|show|give|tell|enumerate|provide)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?',
            r'^(?:i\s+want\s+to\s+know\s+(?:about\s+)?|tell\s+me\s+(?:about\s+)?)',
            r'^(?:is|are)\s+there\s+(?:any\s+)?',
        ]
        for pat in qword_patterns:
            cleaned = re.sub(pat, '', noun_phrase, count=1, flags=re.IGNORECASE).strip()
            if cleaned and len(cleaned) >= 5 and cleaned.lower() != query_lower:
                noun_phrase = cleaned
                break
        
        noun_phrase = re.sub(r'\s+(?:in|on|at|from|for|about|regarding|of)$', '', noun_phrase, flags=re.IGNORECASE).strip()
        
        if noun_phrase.lower() != query_lower and len(noun_phrase) >= 5:
            variants.append(noun_phrase)
        
        stop_words = {
            'what', 'which', 'where', 'when', 'who', 'how', 'why',
            'is', 'are', 'was', 'were', 'do', 'does', 'did',
            'can', 'could', 'would', 'will', 'should', 'shall',
            'has', 'have', 'had', 'having',
            'the', 'a', 'an', 'this', 'that', 'these', 'those',
            'in', 'on', 'at', 'to', 'for', 'from', 'of', 'by', 'with', 'about',
            'and', 'or', 'but', 'not', 'any', 'all', 'some', 'every', 'each',
            'me', 'you', 'i', 'we', 'us', 'my', 'your', 'our',
            'be', 'been', 'being', 'there', 'here',
            'tell', 'show', 'give', 'find', 'list', 'name', 'identify',
            'please', 'also', 'just', 'only',
            'contain', 'contains', 'containing', 'contained',
            'include', 'includes', 'including', 'included',
            'mention', 'mentions', 'mentioning', 'mentioned',
            'require', 'requires', 'requiring', 'required',
            'release', 'releases', 'version', 'versions',
        }
        words = re.findall(r'\b[\w.]+\b', query_lower)
        keywords = [w for w in words if w not in stop_words and len(w) > 1]
        
        if keywords:
            keyword_query = ' '.join(keywords)
            if (keyword_query != query_lower 
                    and len(keyword_query) >= 4
                    and keyword_query not in [v.lower() for v in variants]):
                variants.append(keyword_query)
        
        return variants[:2]

    def _create_cache_key(self, query: str, n_final: int, n_initial: int, filters: Optional[Dict], product: str = None) -> str:
        """Create cache key for query"""
        key_str = f"{query}_{n_final}_{n_initial}_{str(filters)}_{product}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def clear_cache(self):
        """Clear the query cache"""
        if self.query_cache is not None:
            self.query_cache.clear()
            self.cache_hits = 0
            self.cache_misses = 0

    def get_cache_stats(self) -> Dict:
        """Get cache statistics"""
        return {
            "enabled": self.enable_cache,
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "size": len(self.query_cache) if self.query_cache else 0
        }

    def retrieve_enhanced(self, 
                          query: str, 
                          n_final: int = 5,
                          n_initial: int = 20,
                          filters: Optional[Dict] = None,
                          deduplicate: bool = True,
                          conversation_context: str = "",
                          product: str = None) -> Dict:
        """Enhanced retrieval using tailored strategies per query type"""
        if self.enable_cache and self.query_cache is not None:
            cache_key = self._create_cache_key(query, n_final, n_initial, filters, product)
            if cache_key in self.query_cache:
                self.cache_hits += 1
                return self.query_cache[cache_key]
            else:
                self.cache_misses += 1
        
        strategy = self._get_query_strategy(query)
        print(f"[Retrieval] Processing query: '{query}' [Strategy: {strategy['query_type']}, Product: {product}]")
        
        # Adjust initial candidates count based on strategy
        adj_n_initial = max(n_initial, strategy["n_initial"])
        
        # Step 1: Initial vector search
        results = self.retrieve_initial(query, adj_n_initial, filters, product=product, use_exact=strategy["use_exact"])
        
        # Step 1a: Multi-query retrieval
        query_variants = self._generate_query_variants(query)
        if query_variants:
            existing_ids = {r['id'] for r in results}
            variant_limit = max(adj_n_initial // 3, 10)
            for variant in query_variants:
                try:
                    var_results = self.retrieve_initial(variant, variant_limit, filters, product=product, use_exact=strategy["use_exact"])
                    for vr in var_results:
                         if vr['id'] not in existing_ids:
                             results.append(vr)
                             existing_ids.add(vr['id'])
                except Exception as e:
                    print(f"    Warning: Variant retrieval failed: {e}")
                    
        # Step 1b: Supplementary keyword scroll
        if strategy["supplementary_keyword"]:
            keyword_results = self._supplementary_keyword_scroll(query, product=product)
            if keyword_results:
                existing_ids = {r['id'] for r in results}
                for kr in keyword_results:
                    if kr['id'] not in existing_ids:
                        results.append(kr)
                        existing_ids.add(kr['id'])
                        
        # Step 1c: Negation title scroll
        if strategy["negation_title_scroll"]:
            negation_title_markers = [
                'backlog', 'archived', 'archive', 'descoped', 'de-scoped',
                'out of scope', 'out-of-scope', 'excluded', 'exclusion',
                'do not use', 'not in scope', 'not in use', 'deprecated',
                'removed', 'dropped', 'deferred', 'not included',
            ]
            try:
                neg_seen = {r['id'] for r in results}
                for record in self.vector_store.records():
                    metadata = record.get("metadata") or {}
                    if product and metadata.get("product") != product:
                        continue
                    pt_id = str(record.get("id"))
                    if pt_id not in neg_seen:
                        title = get_metadata_field(metadata, 'title', '').lower()
                        if any(marker in title for marker in negation_title_markers):
                            neg_seen.add(pt_id)
                            results.append({
                                'id': pt_id,
                                'content': record.get("content") or record.get("text", ""),
                                'metadata': metadata,
                                'vector_distance': 0.15,
                                'vector_similarity': 0.85,
                                'combined_score': 0.85,
                                'negation_title_match': True
                            })
            except Exception as e:
                print(f"    Warning: Negation title scroll failed: {e}")
                
        if not results:
            return self._empty_results()
            
        # Step 2: Apply BM25 reranking
        if self.use_bm25:
            results = self.apply_bm25_reranking(query, results)
            
        # Step 3: Apply cross-encoder reranking
        if self.use_cross_encoder:
            results = self.apply_cross_encoder_reranking(query, results)
            
        # Step 4: Apply metadata boosting
        results = self.apply_metadata_boosting(results, query)
        
        # Step 5: Calculate combined scores
        results = self.calculate_combined_scores(results, query)
        
        # Step 6: Sort by combined score
        results.sort(key=lambda x: x['combined_score'], reverse=True)
        
        # Step 7: Remove duplicates
        if deduplicate:
            results = self.remove_duplicates(results)
            
        # Step 8: Ensure page diversity
        results = self.ensure_page_diversity(results, max_per_page=strategy["max_per_page"])
        
        # Step 9: Take top N results
        final_results = results[:n_final]
        formatted = self._format_results(query, final_results)
        
        if self.enable_cache and self.query_cache is not None:
            cache_key = self._create_cache_key(query, n_final, n_initial, filters, product)
            self.query_cache[cache_key] = formatted
            
        return formatted

    def _format_results(self, query: str, results: List[Dict]) -> Dict:
        """Format results for consistency with JIRA content sanitization"""
        documents = [sanitize_jira_content(r['content']) for r in results]
        metadatas = [r['metadata'] for r in results]
        distances = [1 - r['combined_score'] for r in results]
        scores = [r['combined_score'] for r in results]
        
        return {
            'query': query,
            'documents': [documents],
            'metadatas': [metadatas],
            'distances': [distances],
            'scores': [scores],
            'raw_results': results
        }

    def _empty_results(self) -> Dict:
        """Return empty results structure"""
        return {
            'query': '',
            'documents': [[]],
            'metadatas': [[]],
            'distances': [[]],
            'scores': [],
            'raw_results': []
        }


def format_context(results: dict, max_length_per_chunk: int = 2500, enable_context_enhancement: bool = None) -> str:
    """Format the retrieved chunks into a single context string with enhanced table support."""
    if not results['documents'] or not results['documents'][0]:
        return "No relevant context found."
    
    if enable_context_enhancement is None:
        enable_context_enhancement = os.getenv("ENABLE_CONTEXT_ENHANCEMENT", "false").lower() == "true"
    
    contexts = []
    total_sources = len(results['documents'][0])
    
    for i, (doc, metadata, score) in enumerate(zip(
        results['documents'][0],
        results['metadatas'][0],
        results['scores'][0] if 'scores' in results else [1.0] * len(results['documents'][0])
    )):
        breadcrumbs = metadata.get('breadcrumbs', '')
        if isinstance(breadcrumbs, list):
            breadcrumb_str = " > ".join(breadcrumbs)
        else:
            breadcrumb_str = breadcrumbs if breadcrumbs else "Root"
        
        if enable_context_enhancement:
            if i < 3:
                effective_max_length = max_length_per_chunk
                relevance_tier = "HIGH RELEVANCE"
            elif i < 7:
                effective_max_length = max_length_per_chunk // 2
                relevance_tier = "MEDIUM RELEVANCE"
            else:
                effective_max_length = max_length_per_chunk // 4
                relevance_tier = "SUPPORTING CONTEXT"
        else:
            effective_max_length = max_length_per_chunk
            relevance_tier = None
        
        content_preview = doc
        if len(doc) > effective_max_length:
            if 'Row-by-Row Details' in doc:
                parts = doc.split('Row-by-Row Details')
                if len(parts) > 1:
                    row_section = 'Row-by-Row Details' + parts[1]
                    if len(row_section) <= effective_max_length:
                        content_preview = row_section
                    else:
                        content_preview = row_section[:effective_max_length] + "..."
                else:
                    content_preview = doc[:effective_max_length] + "..."
            else:
                content_preview = doc[:effective_max_length] + "..."
        
        page_title = get_metadata_field(metadata, 'title', 'Unknown')
        content_type = get_metadata_field(metadata, 'content_type', 'section')
        
        table_info = ""
        has_table = metadata.get('has_table', False) or content_type == 'table' or '| ' in doc
        if has_table:
            table_title = metadata.get('table_title', '')
            table_columns = metadata.get('table_columns', '')
            if table_title:
                table_info = f"\n📋 Table: {table_title}"
            if table_columns:
                table_info += f"\n📊 Columns: {table_columns}"
        
        hierarchical_info = ""
        if enable_context_enhancement:
            parent_headers = metadata.get('parent_headers', '')
            if parent_headers:
                hierarchical_info = f"\n📑 Section Hierarchy: {parent_headers}"
        
        tier_indicator = f"\n⭐ {relevance_tier}" if relevance_tier else ""
        
        context_piece = f"""
[Source {i+1}] Relevance: {score:.1%}{tier_indicator}
─────────────────────────────────────────────────────────
📄 Page: {page_title}
📍 Location: {breadcrumb_str}{hierarchical_info}
🏢 Space: {metadata.get('space_name', 'Unknown')}
🔗 URL: {metadata.get('url', 'N/A')}
📊 Section Type: {content_type}{table_info}
─────────────────────────────────────────────────────────
{content_preview}
─────────────────────────────────────────────────────────
"""
        contexts.append(context_piece)
    
    context_guidance = ""
    if enable_context_enhancement and total_sources > 3:
        context_guidance = f"""
📊 CONTEXT QUALITY GUIDE:
- Sources 1-3: High relevance, primary information (use these first)
- Sources 4-7: Medium relevance, supplementary details
- Sources 8+: Supporting context, use for completeness
================================================================================
"""
    
    header = f"""
📚 RETRIEVED CONTEXT FOR: "{results.get('query', 'Unknown query')}"
Total Sources: {total_sources}
Retrieved at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
================================================================================
{context_guidance}"""
    
    return header + "\n".join(contexts)
