"""
Confluence API Service
Handles fetching pages from Confluence and ingestion process
"""

from atlassian import Confluence
from typing import List, Dict, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fetch_all_pages(confluence: Confluence, space_key: str = None, page_id: str = None, limit: int = 100) -> List[Dict]:
    """
    Fetch all pages from Confluence.
    
    Args:
        confluence: Confluence client instance
        space_key: Specific space key to fetch from (None for all spaces)
        page_id: Specific page ID to fetch (None for all pages)
        limit: Number of pages to fetch per request
    
    Returns:
        List of page dictionaries
    """
    all_pages = []
    
    # If specific page ID is provided
    if page_id:
        try:
            page = confluence.get_page_by_id(
                page_id=page_id,
                expand='body.storage,version,space,history,metadata.labels,_links.webui'
            )
            if page:
                all_pages.append(page)
                logger.info(f"Fetched specific page: {page.get('title')}")
        except Exception as e:
            logger.error(f"Error fetching page {page_id}: {str(e)}")
        return all_pages
    
    # Fetch pages from space or all spaces
    start = 0
    
    while True:
        try:
            if space_key:
                # Fetch pages from specific space
                pages = confluence.get_all_pages_from_space(
                    space=space_key,
                    start=start,
                    limit=limit,
                    expand='body.storage,version,space,history,metadata.labels,_links.webui'
                )
            else:
                # Fetch all pages
                response = confluence.get(
                    '/rest/api/content',
                    params={
                        'type': 'page',
                        'status': 'current',
                        'expand': 'body.storage,version,space,history,metadata.labels,_links.webui',
                        'start': start,
                        'limit': limit
                    }
                )
                pages = response.get('results', [])
            
            if not pages:
                break
            
            all_pages.extend(pages)
            logger.info(f"Fetched {len(pages)} pages (total: {len(all_pages)})")
            
            # Check if there are more pages
            if len(pages) < limit:
                break
            
            start += limit
            
        except Exception as e:
            logger.error(f"Error fetching pages: {str(e)}")
            break
    
    logger.info(f"Total pages fetched: {len(all_pages)}")
    return all_pages


def create_confluence_client(url: str, username: str, api_token: str) -> Confluence:
    """Create and test Confluence client connection"""
    try:
        confluence = Confluence(
            url=url,
            username=username,
            password=api_token,
            cloud=True
        )
        
        # Test connection
        spaces = confluence.get_all_spaces(limit=1)
        logger.info("Successfully connected to Confluence")
        return confluence
        
    except Exception as e:
        logger.error(f"Failed to connect to Confluence: {str(e)}")
        raise


def get_available_spaces(confluence: Confluence) -> List[Dict]:
    """Get list of available spaces"""
    try:
        spaces_response = confluence.get_all_spaces(limit=100)
        spaces = spaces_response.get('results', [])
        return [{'key': s['key'], 'name': s['name']} for s in spaces]
    except Exception as e:
        logger.error(f"Error fetching spaces: {str(e)}")
        return []
