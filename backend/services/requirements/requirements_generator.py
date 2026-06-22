"""
Requirements Generator
======================

Turns an already-ingested Confluence corpus (JSONL vector files populated via
`/api/ingestion/start`) into a structured `requirements.json` suitable for
downstream HLD generation.

Strategy
--------
1. Run a fixed suite of HLD-relevant queries against the existing retriever
   (so we reuse vector + BM25 + cross-encoder retrieval verbatim).
2. Concatenate the top-K context for each query into a single grounded
   evidence blob (with source citations).
3. Make a single LLM call asking the model to emit a strict JSON document
   matching `RequirementsDoc` (defined below).
4. Persist the JSON under `artifacts/` for the HLD generator to consume.

This module is intentionally framework-agnostic: it does NOT touch FastAPI
or chat sessions. Wire it from `routes/requirements.py`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.artifact_store.db import get_vector_store
from services.artifact_store.artifact_paths import artifact_context
from services.shared.llm_client import get_llm_client
from services.confluence.retriever import EnhancedConfluenceRetriever, format_context, get_metadata_field


# ----------------------------------------------------------------------
# Query suite — covers the dimensions an HLD typically needs to capture.
# ----------------------------------------------------------------------
DEFAULT_QUERY_SUITE: List[Dict[str, str]] = [
    {
        "key": "hld_1_1_purpose_scope",
        "query": (
            "Exhaustively extract the purpose and scope of this system. "
            "Do NOT summarize or generalize. Find and extract: "
            "1) The complete business background, target users, problem statement, and core objectives. "
            "2) Every explicit 'IN SCOPE' feature, user story, API endpoint, component, and functional capability. "
            "3) Every explicit 'OUT OF SCOPE' item, future phase plan, or non-goal. "
            "4) Project success metrics, business goals, and KPIs. "
            "Extract every detail, even if mentioned only in passing."
        ),
    },
    {
        "key": "hld_1_2_definitions_acronyms",
        "query": (
            "Scan the entire document to extract every single term, abbreviation, technical acronym, "
            "system-specific shorthand, domain jargon, or business concept. "
            "For each term, extract its exact literal expansion and a comprehensive, detailed "
            "explanation of what it means in the context of this system. Do not omit any term, "
            "no matter how common."
        ),
    },
    {
        "key": "hld_1_3_references",
        "query": (
            "Locate and extract all references, citations, external links, internal links, "
            "Confluence page links, PRDs, RFCs, API contracts, architectural designs, code repositories, "
            "standard documents, or external product specs mentioned. "
            "Capture: 1) Document Title / Resource Name, 2) URL or Location, 3) Exhaustive description "
            "of why it is referenced and how it relates to this system."
        ),
    },
    {
        "key": "hld_1_4_context",
        "query": (
            "Extract the exact enterprise context and structural environment of the system. "
            "Identify: 1) What ecosystem, pipeline, or broader company system does this system live in? "
            "2) What are the immediate upstream and downstream systems, consumers, or trigger sources? "
            "3) How does data enter the system, and where does it flow out? "
            "4) What are the legacy systems or processes being replaced, modified, or integrated with?"
        ),
    },
    {
        "key": "hld_2_1_logical_view",
        "query": (
            "This query is critical for generating code-based diagrams using graphify and mermaid. "
            "Identify and detail every single high-level module, sub-system, microservice, utility, "
            "database, data store, layer, third-party component, or actor. "
            "For each element, extract: "
            "1) Its exact name, technology stack (if mentioned), and detailed architectural role. "
            "2) Every capability, internal sub-component, and responsibility it holds. "
            "3) Detailed step-by-step transaction/processing sequence flows. Write exactly how "
            "Component A calls Component B, what method/API is hit, what data payload is sent, "
            "how Component B processes it, and how it responds. "
            "Ensure the flow sequence is complete, explicit, and logical to enable direct translation "
            "into a Mermaid sequence or component diagram."
        ),
    },
    {
        "key": "hld_3_security_approach",
        "query": (
            "Extract the entire security architecture, rules, and protocols. "
            "Do NOT summarize. Extract exact details for: "
            "1) Authentication (AuthN): protocols used (OAuth2, OIDC, SAML, basic, API Keys), identity "
            "providers (Okta, Keycloak, Auth0), token validation, signature checks, and TTLs. "
            "2) Authorization (AuthZ): models (RBAC, ABAC), roles, permissions, scopes, and rule structures. "
            "3) Data Protection: exact encryption algorithms and protocols for data-in-transit (TLS 1.2/1.3, HTTPS, mTLS) "
            "and data-at-rest (AES-256, column-level, database transparent encryption). "
            "4) Secrets Management: how API keys, certificates, database passwords, and environment secrets are managed "
            "(HashiCorp Vault, AWS Secrets Manager, AWS SSM). "
            "5) Compliance & Auditing: handling of PII/PHI, compliance regulations (GDPR, HIPAA, SOC2, PCI-DSS), "
            "and detailed audit logging specifications (what is logged, metadata captured, log masking/redaction)."
        ),
    },
    {
        "key": "hld_4_scalability_view",
        "query": (
            "Extract all quantitative and qualitative non-functional scaling and performance metrics. "
            "Extract exact numbers, units, limits, and patterns: "
            "1) Traffic and throughput targets (Requests Per Second [RPS], Daily/Monthly Active Users, "
            "concurrency bounds, peak loads, and event/message stream ingress volumes). "
            "2) Performance thresholds: target SLAs/SLOs, latency targets (e.g., p95 < 200ms, p99 < 500ms), "
            "timeouts, retry budgets, and exponential backoff profiles. "
            "3) Storage & Data Scaling: expected data size growth (GB/TB/PB per month), retention guidelines, "
            "archival strategies, read/write ratios, database partitioning/sharding details, and caching layers "
            "(Redis caching patterns, TTL, eviction policies). "
            "4) Load Balancing and clustering details."
        ),
    },
    {
        "key": "hld_5_infrastructure_view",
        "query": (
            "Extract the deployment target, cloud orchestration, and topology configurations. "
            "Identify: "
            "1) Deployment platforms: cloud providers (AWS, GCP, Azure), on-premises environments, or hybrid structures. "
            "2) Compute and runtime: container/orchestration engines (Kubernetes, EKS, ECS, Docker), serverless (Lambda, Cloud Functions), "
            "or VM topologies. "
            "3) Network structure: VPC layouts, subnets, DMZs, API Gateways, reverse proxies, reverse-SSH tunnels, "
            "load balancers, firewalls, and route mechanisms. "
            "4) Disaster Recovery (DR) & High Availability (HA): Multi-region, Multi-AZ setups, active-active or active-passive routing, "
            "RTO (Recovery Time Objective) and RPO (Recovery Point Objective) metrics, back-up schedules, and automatic failovers. "
            "5) CI/CD & Observability: build tools, Infrastructure as Code (Terraform, CloudFormation), monitoring stacks "
            "(Prometheus, Grafana, Datadog), and logging infrastructure."
        ),
    },
]


# ----------------------------------------------------------------------
# Structured output schema (documented inline for LLM grounding)
# ----------------------------------------------------------------------
REQUIREMENTS_JSON_SCHEMA = """
{
  "project_name": "<string>",
  "hld_content": {
    "1_introduction": {
      "1_1_purpose_and_scope": {
        "business_problem_statement": "<extremely detailed multi-paragraph narrative of business/technical drivers, issues, and background>",
        "system_objectives": ["<granular list of explicit architectural and business objectives>"],
        "in_scope": ["<comprehensive list of every functional module, component, user action, and capability explicitly in scope>"],
        "out_scope": ["<comprehensive list of every out-of-scope system boundary, non-goal, future phase item, and limitation>"],
        "success_criteria": ["<measurable metrics, targets, business values, or KPIs denoting success>"]
      },
      "1_2_definitions_and_acronyms": [
        {
          "term": "<string>",
          "expansion": "<full literal acronym/abbreviation expansion>",
          "definition": "<highly detailed, context-aware functional explanation of the term>"
        }
      ],
      "1_3_references": [
        {
          "title": "<document or resource title>",
          "url_or_location": "<exact url or directory location if specified, else omit field>",
          "relationship_description": "<comprehensive explanation of how this resource guides, dictates, or links to the current HLD>"
        }
      ],
      "1_4_context": {
        "ecosystem_description": "<detailed narrative showing how the system fits into the broader enterprise, its domain context, and high-level role>",
        "upstream_dependencies": [
          {
            "system_name": "<string>",
            "trigger_event": "<what action triggers the communication>",
            "mechanism": "<REST/gRPC/Kafka/Webhook/etc.>",
            "details": "<how the upstream system affects or initiates flows in this system>"
          }
        ],
        "downstream_consumers": [
          {
            "system_name": "<string>",
            "mechanism": "<REST/gRPC/Kafka/Webhook/etc.>",
            "data_transmitted": "<fields, files, or messages transmitted>",
            "details": "<how this system feeds or triggers downstream workflows>"
          }
        ]
      }
    },
    "2_logical_view": {
      "modules": [
        {
          "module_name": "<string (e.g., Auth Service, Analytics Engine)>",
          "architectural_layer": "<e.g., API Gateway, Controller, Core Service, Data Storage, Message Broker>",
          "detailed_responsibility": "<comprehensive 3-5 sentence explanation of exactly what this module owns and is responsible for>",
          "capabilities": ["<granular capabilities, endpoints, or features provided>"],
          "interfaces_and_apis": [
            {
              "interface_name": "<string>",
              "protocol_or_type": "HTTP/REST|gRPC|GraphQL|Kafka Topic|AMQP|Internal Class",
              "signature": "<e.g., POST /v1/users or publish_event(user_created)>",
              "description": "<detailed explanation of what this interface does, including schema/payload references if documented>"
            }
          ],
          "dependencies": ["<list of internal/external modules or services this module depends on to function>"]
        }
      ],
      "interactions_and_flows": [
        {
          "flow_name": "<string (e.g., User Login and Session Initialization)>",
          "trigger": "<string (e.g., user submits credentials via login form)>",
          "step_by_step_sequence": [
            {
              "step_number": "<integer>",
              "source_component": "<actor or module calling>",
              "destination_component": "<module or database being called>",
              "operation_signature": "<method or route being executed>",
              "payload_description": "<details of arguments or fields passed>",
              "description": "<rich description of what processing occurs at this step, including error/validation checks>"
            }
          ]
        }
      ]
    },
    "3_security_approach": {
      "authentication": {
        "protocol": "<detailed description of protocols (OIDC, JWT, SAML, OAuth2, API Keys) used>",
        "identity_provider": "<identity provider engine and setup (e.g., Okta, Auth0, AWS Cognito)>",
        "token_management": "<creation, payload contents, verification, TTLs, signature algorithms, and rotation rules>"
      },
      "authorization": {
        "model": "RBAC|ABAC|ACL|Other",
        "description": "<detailed text on authorization setup>",
        "roles_and_permissions": [
          {
            "role": "<string>",
            "allowed_actions": ["<string>"],
            "constraints": "<string (e.g., user must belong to organization X)>"
          }
        ]
      },
      "data_protection": {
        "in_transit": "<detailed narrative on transport layer encryption, TLS version, mTLS setups, and boundary ingress points>",
        "at_rest": "<detailed narrative on database/volume encryption, key sizes, transparent database encryption (TDE), and partition-level algorithms>",
        "secrets_management": "<detailed narrative on how secrets/keys/credentials are stored, injected, rotated, and isolated (Vault, AWS Secrets Manager)>",
        "pii_and_privacy": "<detailed handling of personally identifiable information (PII), masking, hashing, anonymization, and isolated storage schemas>"
      },
      "compliance_and_auditing": {
        "compliance_standards": ["<e.g., GDPR, HIPAA, SOC2, PCI-DSS, ISO27001>"],
        "audit_logging_specifications": "<what specific transactions, data updates, and authentication events must be logged, what metadata is captured, where logs are sent, and how security logs are protected>"
      }
    },
    "4_scalability_view": {
      "performance_targets": {
        "expected_requests_per_second": "<string / quantitative metric>",
        "target_latency_percentiles": "<p50/p95/p99 target latencies for core pathways>",
        "concurrency_bounds": "<maximum simultaneous connections or parallel operations supported>",
        "throughput_narrative": "<highly detailed narrative describing peak vs average loads, growth expectations, and SLAs>"
      },
      "bottlenecks_and_mitigations": {
        "caching_strategy": "<detailed text detailing caching layers (Redis, Memcached), topology, hit-rate targets, TTLs, write-through vs read-through, and evictions>",
        "db_scaling": "<detailed text describing indexing, replica offloading, horizontal sharding, connection pooling, and multi-write active clustering>",
        "asynchronous_processing": "<detailed narrative regarding message brokers, event streaming (Kafka/RabbitMQ), backpressure handling, queue thresholds, and dead-letter queues>"
      },
      "data_retention_and_volume": {
        "volume_estimates": "<estimated data storage footprint generated per week/month/year>",
        "retention_and_pruning_policy": "<how long data sits in operational DBs before cold-storage archiving or permanent deletion>"
      }
    },
    "5_infrastructure_view": {
      "deployment_target": {
        "hosting_platform": "<e.g., AWS, GCP, Azure, Hybrid, Bare Metal>",
        "runtime_orchestration": "<e.g., Kubernetes, EKS, ECS, serverless AWS Lambda, Docker Swarm>",
        "environment_topology": "<detailed textual structure of development, staging, sandbox, and production clusters>"
      },
      "topology_components": [
        {
          "component_name": "<string>",
          "infrastructure_type": "compute|database|cache|message_broker|load_balancer|api_gateway|dns_routing|firewall_security",
          "technology": "<e.g., AWS EKS, PostgreSQL, Redis, Apache Kafka, NGINX, Route 53>",
          "redundancy_and_ha": "<e.g., multi-AZ clustering, active-passive hot-standby, auto-recovery scales, horizontal pods scale bounds>"
        }
      ],
      "networking_and_connectivity": {
        "network_isolation": "<detailed narrative on VPC configurations, public vs private subnets, bastion hosts, NAT gateways, DMZ, and internal routing limits>",
        "ingress_egress_security": "<API Gateways, Application Load Balancers, Cloudflare WAF configurations, Reverse Proxies, and egress filtering rules>"
      },
      "resilience_and_disaster_recovery": {
        "rto_target": "<RTO quantitative limit>",
        "rpo_target": "<RPO quantitative limit>",
        "failover_mechanism": "<detailed explanation of multi-region, DNS routing failover (e.g., Route 53 latency routing), database failovers, and backup schedules>"
      }
    }
  },
  "source_pages_mapping": [
    {
      "section": "<e.g., 2_logical_view>",
      "source_titles": ["<title>"]
    }
  ]
}
""".strip()


# ----------------------------------------------------------------------
# Partial schemas — one per LLM batch to stay within output-token limits.
# ----------------------------------------------------------------------
REQUIREMENTS_JSON_SCHEMA_PART1 = """
{
  "project_name": "<string>",
  "hld_content": {
    "1_introduction": {
      "1_1_purpose_and_scope": {
        "business_problem_statement": "<extremely detailed multi-paragraph narrative>",
        "system_objectives": ["<granular architectural and business objectives>"],
        "in_scope": ["<every functional module, component, user action, and capability explicitly in scope>"],
        "out_scope": ["<every out-of-scope boundary, non-goal, future phase item, and limitation>"],
        "success_criteria": ["<measurable metrics, targets, KPIs denoting success>"]
      },
      "1_2_definitions_and_acronyms": [
        {
          "term": "<string>",
          "expansion": "<full literal acronym/abbreviation expansion>",
          "definition": "<highly detailed, context-aware functional explanation of the term>"
        }
      ],
      "1_3_references": [
        {
          "title": "<document or resource title>",
          "url_or_location": "<exact url or directory location if specified, else omit field>",
          "relationship_description": "<comprehensive explanation of how this resource links to the HLD>"
        }
      ],
      "1_4_context": {
        "ecosystem_description": "<detailed narrative of how the system fits into the broader enterprise>",
        "upstream_dependencies": [
          {
            "system_name": "<string>",
            "trigger_event": "<what triggers communication>",
            "mechanism": "<REST/gRPC/Kafka/Webhook/etc.>",
            "details": "<how the upstream system affects this system>"
          }
        ],
        "downstream_consumers": [
          {
            "system_name": "<string>",
            "mechanism": "<REST/gRPC/Kafka/Webhook/etc.>",
            "data_transmitted": "<fields, files, or messages transmitted>",
            "details": "<how this system feeds or triggers downstream workflows>"
          }
        ]
      }
    }
  },
  "source_pages_mapping": [
    {
      "section": "<e.g., 1_introduction>",
      "source_titles": ["<title>"]
    }
  ]
}
""".strip()

REQUIREMENTS_JSON_SCHEMA_PART2 = """
{
  "project_name": "<string>",
  "hld_content": {
    "2_logical_view": {
      "modules": [
        {
          "module_name": "<string (e.g., Auth Service, Analytics Engine)>",
          "architectural_layer": "<e.g., API Gateway, Controller, Core Service, Data Storage, Message Broker>",
          "detailed_responsibility": "<comprehensive 3-5 sentence explanation of exactly what this module owns>",
          "capabilities": ["<granular capabilities, endpoints, or features provided>"],
          "interfaces_and_apis": [
            {
              "interface_name": "<string>",
              "protocol_or_type": "HTTP/REST|gRPC|GraphQL|Kafka Topic|AMQP|Internal Class",
              "signature": "<e.g., POST /v1/users or publish_event(user_created)>",
              "description": "<detailed explanation including schema/payload references if documented>"
            }
          ],
          "dependencies": ["<internal/external modules or services this module depends on>"]
        }
      ],
      "interactions_and_flows": [
        {
          "flow_name": "<string (e.g., User Login and Session Initialization)>",
          "trigger": "<string (e.g., user submits credentials via login form)>",
          "step_by_step_sequence": [
            {
              "step_number": "<integer>",
              "source_component": "<actor or module calling>",
              "destination_component": "<module or database being called>",
              "operation_signature": "<method or route being executed>",
              "payload_description": "<details of arguments or fields passed>",
              "description": "<rich description of what processing occurs at this step, including error/validation checks>"
            }
          ]
        }
      ]
    }
  },
  "source_pages_mapping": [
    {
      "section": "<e.g., 2_logical_view>",
      "source_titles": ["<title>"]
    }
  ]
}
""".strip()

REQUIREMENTS_JSON_SCHEMA_PART3 = """
{
  "project_name": "<string>",
  "hld_content": {
    "3_security_approach": {
      "authentication": {
        "protocol": "<detailed protocols: OIDC, JWT, SAML, OAuth2, API Keys>",
        "identity_provider": "<identity provider engine and setup>",
        "token_management": "<creation, payload, verification, TTLs, signature algorithms, rotation rules>"
      },
      "authorization": {
        "model": "RBAC|ABAC|ACL|Other",
        "description": "<detailed text on authorization setup>",
        "roles_and_permissions": [
          {
            "role": "<string>",
            "allowed_actions": ["<string>"],
            "constraints": "<string>"
          }
        ]
      },
      "data_protection": {
        "in_transit": "<TLS version, mTLS setups, boundary ingress encryption details>",
        "at_rest": "<database/volume encryption, key sizes, TDE, partition-level algorithms>",
        "secrets_management": "<how secrets/keys/credentials are stored, injected, rotated (Vault, AWS Secrets Manager)>",
        "pii_and_privacy": "<PII handling: masking, hashing, anonymization, isolated storage schemas>"
      },
      "compliance_and_auditing": {
        "compliance_standards": ["<e.g., GDPR, HIPAA, SOC2, PCI-DSS, ISO27001>"],
        "audit_logging_specifications": "<what transactions/events are logged, metadata captured, where logs are sent, how protected>"
      }
    },
    "4_scalability_view": {
      "performance_targets": {
        "expected_requests_per_second": "<quantitative metric>",
        "target_latency_percentiles": "<p50/p95/p99 target latencies for core pathways>",
        "concurrency_bounds": "<maximum simultaneous connections or parallel operations>",
        "throughput_narrative": "<detailed narrative: peak vs average loads, growth expectations, SLAs>"
      },
      "bottlenecks_and_mitigations": {
        "caching_strategy": "<caching layers (Redis, Memcached), topology, TTLs, write-through vs read-through, evictions>",
        "db_scaling": "<indexing, replica offloading, horizontal sharding, connection pooling, multi-write clustering>",
        "asynchronous_processing": "<message brokers, event streaming (Kafka/RabbitMQ), backpressure, dead-letter queues>"
      },
      "data_retention_and_volume": {
        "volume_estimates": "<estimated storage footprint per week/month/year>",
        "retention_and_pruning_policy": "<how long data sits in operational DBs before archiving or deletion>"
      }
    },
    "5_infrastructure_view": {
      "deployment_target": {
        "hosting_platform": "<e.g., AWS, GCP, Azure, Hybrid, Bare Metal>",
        "runtime_orchestration": "<e.g., Kubernetes, EKS, ECS, serverless AWS Lambda, Docker Swarm>",
        "environment_topology": "<structure of development, staging, sandbox, and production clusters>"
      },
      "topology_components": [
        {
          "component_name": "<string>",
          "infrastructure_type": "compute|database|cache|message_broker|load_balancer|api_gateway|dns_routing|firewall_security",
          "technology": "<e.g., AWS EKS, PostgreSQL, Redis, Apache Kafka, NGINX, Route 53>",
          "redundancy_and_ha": "<multi-AZ clustering, active-passive hot-standby, auto-recovery scale bounds>"
        }
      ],
      "networking_and_connectivity": {
        "network_isolation": "<VPC configs, public/private subnets, bastion hosts, NAT gateways, DMZ, internal routing>",
        "ingress_egress_security": "<API Gateways, ALBs, Cloudflare WAF, Reverse Proxies, egress filtering rules>"
      },
      "resilience_and_disaster_recovery": {
        "rto_target": "<RTO quantitative limit>",
        "rpo_target": "<RPO quantitative limit>",
        "failover_mechanism": "<multi-region, DNS routing failover, database failovers, backup schedules>"
      }
    }
  },
  "source_pages_mapping": [
    {
      "section": "<e.g., 3_security_approach>",
      "source_titles": ["<title>"]
    }
  ]
}
""".strip()

# Ordered list of (query-subset, schema) pairs used in 3-batch mode.
_PARTIAL_SCHEMAS: List[str] = [
    REQUIREMENTS_JSON_SCHEMA_PART1,
    REQUIREMENTS_JSON_SCHEMA_PART2,
    REQUIREMENTS_JSON_SCHEMA_PART3,
]

# Split the default suite into three thematic batches.
# Part 1 → Introduction (queries 0-3)
# Part 2 → Logical View (query 4)
# Part 3 → Security + Scalability + Infrastructure (queries 5-6)
QUERY_SUITE_PARTS: List[List[Dict[str, str]]] = [
    DEFAULT_QUERY_SUITE[0:4],
    DEFAULT_QUERY_SUITE[4:5],
    DEFAULT_QUERY_SUITE[5:7],
]


@dataclass
class EvidenceItem:
    query_key: str
    query: str
    sources: List[Dict[str, Any]] = field(default_factory=list)
    context_text: str = ""


@dataclass
class RequirementsResult:
    job_id: str
    product: Optional[str]
    release: Optional[str]
    started_at: str
    completed_at: str
    requirements: Dict[str, Any]
    evidence: List[EvidenceItem]
    artifact_path: str


def _build_evidence(
    retriever: EnhancedConfluenceRetriever,
    queries: List[Dict[str, str]],
    *,
    n_results: int,
    product: Optional[str],
) -> List[EvidenceItem]:
    """Run each query through the retriever and capture top-K hits + context.

    `retrieve_enhanced` returns a ChromaDB-style nested dict
    ({"documents": [[...]], "metadatas": [[...]], "scores": [[...]]}).
    """
    evidence: List[EvidenceItem] = []
    for q in queries:
        try:
            results = retriever.retrieve_enhanced(
                query=q["query"],
                n_final=n_results,
                n_initial=max(n_results * 4, 20),
                deduplicate=True,
                product=product,
            )
        except TypeError:
            # Older retriever signatures may not accept `product`
            results = retriever.retrieve_enhanced(
                query=q["query"],
                n_final=n_results,
                n_initial=max(n_results * 4, 20),
                deduplicate=True,
            )

        results = results or {"documents": [[]], "metadatas": [[]], "scores": [[]]}
        context_text = format_context(results, enable_context_enhancement=False)

        sources: List[Dict[str, Any]] = []
        metadatas = (results.get("metadatas") or [[]])[0]
        scores = (results.get("scores") or [[]])[0]
        for idx, md in enumerate(metadatas):
            md = md or {}
            score = float(scores[idx]) if idx < len(scores) else 0.0
            sources.append(
                {
                    "title": get_metadata_field(md, "title") or "Unknown",
                    "url": get_metadata_field(md, "url") or get_metadata_field(md, "page_url") or "",
                    "page_id": get_metadata_field(md, "page_id") or "",
                    "score": score,
                }
            )

        # Deduplicate sources by title while preserving order
        seen: set = set()
        unique_sources: List[Dict[str, Any]] = []
        for s in sources:
            key = (s["title"], s["url"])
            if key in seen:
                continue
            seen.add(key)
            unique_sources.append(s)

        evidence.append(
            EvidenceItem(
                query_key=q["key"],
                query=q["query"],
                sources=unique_sources,
                context_text=context_text,
            )
        )
    return evidence


def _build_llm_prompt(
    evidence: List[EvidenceItem],
    product: Optional[str],
    *,
    schema: str = REQUIREMENTS_JSON_SCHEMA,
) -> str:
    parts: List[str] = []
    parts.append(
        "You are an Elite Enterprise Architect specializing in transforming legacy/functional Confluence "
        "requirements into structured, detail-heavy architectural specification files. These specifications "
        "will feed an automated layout engine to build an exhaustive, publication-grade High Level Design (HLD) document."
    )
    if product:
        parts.append(f"Target product/scope context: **{product}**.")
    parts.append("")
    parts.append("=== STRICT EXTRACTION AND COMPOSTION RULES ===")
    parts.append(
        "1. NO SUMMARIZATION: Your greatest failure is summarizing details away. Do not compress multiple services into generic phrases.\n"
        "   - Bad: 'The system uses security protocols and logs events.'\n"
        "   - Good: 'The system implements OAuth2 Authorization Code Flow with PKCE via Okta, generating TLS 1.3 encrypted JWT tokens and audit logging all DB write mutations in log-broker-prod.'\n"
        "2. EXHAUSTIVE COVERAGE: Extract all facts, names, URLs, schemas, latency targets, throughput requirements, "
        "security standards, technology choices, environment targets, and definitions found in the context.\n"
        "3. STRUCTURAL SEQUENCING: Under '2_logical_view' -> 'interactions_and_flows', construct exact step-by-step transaction flows. "
        "Each step must identify the sender, receiver, execution mechanism, and payload details. This is mandatory for the automated Mermaid rendering pipeline.\n"
        "4. EXCLUDE MISSING DATA SAFELY: If the evidence does not provide data for a specific schema field, do NOT write filler "
        "like 'Not specified', 'Unknown', 'N/A' or invent fake entities. Leave arrays empty, omit Optional objects, or use empty strings.\n"
        "5. EVIDENCE ONLY: Base 100% of your response on the provided Confluence context. Do not invent metrics or technologies not present in the evidence.\n"
        "6. PURE JSON OUTPUT: Output ONLY a single, valid JSON object matching the schema below. Do not wrap the JSON inside "
        "markdown code fences (no ```json or ``` blocks), do not add lead-in conversational prose, and do not include post-prose commentary."
    )
    parts.append("")
    parts.append("=== TARGET SPECIFICATION JSON SCHEMA ===")
    parts.append(schema)
    parts.append("")
    parts.append("=== CONFLUENCE EVIDENCE BLOCKS ===")
    for item in evidence:
        parts.append(f"\n--- [QUERY BLOCK: {item.query_key}] Prompt: {item.query} ---")
        if item.context_text:
            parts.append(item.context_text)
        else:
            parts.append("(Empty: No matching documentation context found in index for this block.)")
    parts.append("\n=== END OF CONFLUENCE EVIDENCE ===")
    parts.append("")
    parts.append("Compile all retrieved evidence into the strict target JSON format. Execute now:")
    return "\n".join(parts)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _merge_partial_requirements(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deep-merge partial requirement dicts produced by separate LLM batches.

    Each part covers a distinct set of ``hld_content`` keys, so a simple
    ``dict.update`` on ``hld_content`` is sufficient.  ``source_pages_mapping``
    lists are concatenated; ``project_name`` is taken from the first non-empty
    part.
    """
    merged: Dict[str, Any] = {
        "project_name": "",
        "hld_content": {},
        "source_pages_mapping": [],
    }
    for part in parts:
        if not merged["project_name"] and part.get("project_name"):
            merged["project_name"] = part["project_name"]
        merged["hld_content"].update(part.get("hld_content") or {})
        merged["source_pages_mapping"].extend(part.get("source_pages_mapping") or [])
    return merged


def _requirements_to_markdown(payload: Dict[str, Any]) -> str:
    """Convert a requirements payload dict into a human-readable Markdown document."""
    req: Dict[str, Any] = payload.get("requirements", {})
    hld: Dict[str, Any] = req.get("hld_content", {})
    lines: List[str] = []

    project_name = req.get("project_name", payload.get("product") or "Untitled Project")
    lines.append(f"# Requirements: {project_name}\n")
    lines.append(
        f"**Job ID:** `{payload.get('job_id', '')}` | "
        f"**Generated:** {payload.get('completed_at', '')}  \n"
    )
    lines.append("---\n")

    # ── 1. Introduction ──────────────────────────────────────────────────────
    intro: Dict[str, Any] = hld.get("1_introduction", {})
    lines.append("## 1. Introduction\n")

    # 1.1 Purpose and Scope
    ps: Dict[str, Any] = intro.get("1_1_purpose_and_scope", {})
    if ps:
        lines.append("### 1.1 Purpose and Scope\n")
        if ps.get("business_problem_statement"):
            lines.append(f"{ps['business_problem_statement']}\n")
        for label, key in [
            ("**System Objectives**", "system_objectives"),
            ("**In Scope**", "in_scope"),
            ("**Out of Scope**", "out_scope"),
            ("**Success Criteria**", "success_criteria"),
        ]:
            items: List[str] = ps.get(key) or []
            if items:
                lines.append(f"\n{label}\n")
                for item in items:
                    lines.append(f"- {item}")
                lines.append("")

    # 1.2 Definitions and Acronyms
    defs: List[Dict[str, Any]] = intro.get("1_2_definitions_and_acronyms") or []
    if defs:
        lines.append("### 1.2 Definitions and Acronyms\n")
        lines.append("| Term | Expansion | Definition |")
        lines.append("|------|-----------|------------|")
        for d in defs:
            term = (d.get("term") or "").replace("|", "\\|")
            exp = (d.get("expansion") or "").replace("|", "\\|")
            defn = (d.get("definition") or "").replace("|", "\\|")
            lines.append(f"| {term} | {exp} | {defn} |")
        lines.append("")

    # 1.3 References
    refs: List[Dict[str, Any]] = intro.get("1_3_references") or []
    if refs:
        lines.append("### 1.3 References\n")
        for r in refs:
            title = r.get("title") or ""
            url = r.get("url_or_location") or ""
            desc = r.get("relationship_description") or ""
            link = f"[{title}]({url})" if url else title
            lines.append(f"- **{link}** — {desc}")
        lines.append("")

    # 1.4 Context
    ctx: Dict[str, Any] = intro.get("1_4_context", {})
    if ctx:
        lines.append("### 1.4 Context\n")
        if ctx.get("ecosystem_description"):
            lines.append(f"{ctx['ecosystem_description']}\n")
        ups: List[Dict[str, Any]] = ctx.get("upstream_dependencies") or []
        if ups:
            lines.append("**Upstream Dependencies**\n")
            for u in ups:
                lines.append(
                    f"- **{u.get('system_name', '')}** ({u.get('mechanism', '')}) — "
                    f"Trigger: {u.get('trigger_event', '')}. {u.get('details', '')}"
                )
            lines.append("")
        downs: List[Dict[str, Any]] = ctx.get("downstream_consumers") or []
        if downs:
            lines.append("**Downstream Consumers**\n")
            for dw in downs:
                lines.append(
                    f"- **{dw.get('system_name', '')}** ({dw.get('mechanism', '')}) — "
                    f"Data: {dw.get('data_transmitted', '')}. {dw.get('details', '')}"
                )
            lines.append("")

    # ── 2. Logical View ──────────────────────────────────────────────────────
    lv: Dict[str, Any] = hld.get("2_logical_view", {})
    if lv:
        lines.append("## 2. Logical View\n")
        modules: List[Dict[str, Any]] = lv.get("modules") or []
        if modules:
            lines.append("### Modules\n")
            for mod in modules:
                lines.append(f"#### {mod.get('module_name', 'Module')}\n")
                lines.append(f"**Layer:** {mod.get('architectural_layer', '')}  ")
                lines.append(f"**Responsibility:** {mod.get('detailed_responsibility', '')}  \n")
                caps: List[str] = mod.get("capabilities") or []
                if caps:
                    lines.append("**Capabilities:**")
                    for c in caps:
                        lines.append(f"- {c}")
                    lines.append("")
                apis: List[Dict[str, Any]] = mod.get("interfaces_and_apis") or []
                if apis:
                    lines.append("**Interfaces & APIs:**")
                    lines.append("| Interface | Protocol | Signature | Description |")
                    lines.append("|-----------|----------|-----------|-------------|")
                    for api in apis:
                        n = (api.get("interface_name") or "").replace("|", "\\|")
                        p = (api.get("protocol_or_type") or "").replace("|", "\\|")
                        s = (api.get("signature") or "").replace("|", "\\|")
                        desc = (api.get("description") or "").replace("|", "\\|")
                        lines.append(f"| {n} | {p} | {s} | {desc} |")
                    lines.append("")
                deps: List[str] = mod.get("dependencies") or []
                if deps:
                    lines.append(f"**Dependencies:** {', '.join(deps)}\n")

        flows: List[Dict[str, Any]] = lv.get("interactions_and_flows") or []
        if flows:
            lines.append("### Interactions & Flows\n")
            for flow in flows:
                lines.append(f"#### {flow.get('flow_name', 'Flow')}\n")
                lines.append(f"**Trigger:** {flow.get('trigger', '')}  \n")
                steps: List[Dict[str, Any]] = flow.get("step_by_step_sequence") or []
                if steps:
                    lines.append("| Step | Source | Destination | Operation | Payload | Description |")
                    lines.append("|------|--------|-------------|-----------|---------|-------------|")
                    for step in steps:
                        num = step.get("step_number", "")
                        src = (step.get("source_component") or "").replace("|", "\\|")
                        dst = (step.get("destination_component") or "").replace("|", "\\|")
                        op = (step.get("operation_signature") or "").replace("|", "\\|")
                        pay = (step.get("payload_description") or "").replace("|", "\\|")
                        dsc = (step.get("description") or "").replace("|", "\\|")
                        lines.append(f"| {num} | {src} | {dst} | {op} | {pay} | {dsc} |")
                    lines.append("")

    # ── 3. Security Approach ─────────────────────────────────────────────────
    sec: Dict[str, Any] = hld.get("3_security_approach", {})
    if sec:
        lines.append("## 3. Security Approach\n")
        auth_n: Dict[str, Any] = sec.get("authentication", {})
        if auth_n:
            lines.append("### Authentication\n")
            lines.append(f"- **Protocol:** {auth_n.get('protocol', '')}")
            lines.append(f"- **Identity Provider:** {auth_n.get('identity_provider', '')}")
            lines.append(f"- **Token Management:** {auth_n.get('token_management', '')}\n")
        auth_z: Dict[str, Any] = sec.get("authorization", {})
        if auth_z:
            lines.append("### Authorization\n")
            lines.append(f"- **Model:** {auth_z.get('model', '')}")
            lines.append(f"- {auth_z.get('description', '')}")
            roles: List[Dict[str, Any]] = auth_z.get("roles_and_permissions") or []
            if roles:
                lines.append("\n**Roles & Permissions:**")
                lines.append("| Role | Allowed Actions | Constraints |")
                lines.append("|------|-----------------|-------------|")
                for role in roles:
                    r = (role.get("role") or "").replace("|", "\\|")
                    a = ", ".join(role.get("allowed_actions") or []).replace("|", "\\|")
                    c = (role.get("constraints") or "").replace("|", "\\|")
                    lines.append(f"| {r} | {a} | {c} |")
                lines.append("")
        dp: Dict[str, Any] = sec.get("data_protection", {})
        if dp:
            lines.append("### Data Protection\n")
            lines.append(f"- **In Transit:** {dp.get('in_transit', '')}")
            lines.append(f"- **At Rest:** {dp.get('at_rest', '')}")
            lines.append(f"- **Secrets Management:** {dp.get('secrets_management', '')}")
            lines.append(f"- **PII & Privacy:** {dp.get('pii_and_privacy', '')}\n")
        ca: Dict[str, Any] = sec.get("compliance_and_auditing", {})
        if ca:
            lines.append("### Compliance & Auditing\n")
            stds = ca.get("compliance_standards") or []
            if stds:
                lines.append(f"**Standards:** {', '.join(stds)}  ")
            lines.append(f"**Audit Logging:** {ca.get('audit_logging_specifications', '')}\n")

    # ── 4. Scalability View ──────────────────────────────────────────────────
    scal: Dict[str, Any] = hld.get("4_scalability_view", {})
    if scal:
        lines.append("## 4. Scalability View\n")
        pt: Dict[str, Any] = scal.get("performance_targets", {})
        if pt:
            lines.append("### Performance Targets\n")
            lines.append(f"- **RPS:** {pt.get('expected_requests_per_second', '')}")
            lines.append(f"- **Latency:** {pt.get('target_latency_percentiles', '')}")
            lines.append(f"- **Concurrency:** {pt.get('concurrency_bounds', '')}")
            lines.append(f"- **Throughput Narrative:** {pt.get('throughput_narrative', '')}\n")
        bm: Dict[str, Any] = scal.get("bottlenecks_and_mitigations", {})
        if bm:
            lines.append("### Bottlenecks & Mitigations\n")
            lines.append(f"- **Caching:** {bm.get('caching_strategy', '')}")
            lines.append(f"- **DB Scaling:** {bm.get('db_scaling', '')}")
            lines.append(f"- **Async Processing:** {bm.get('asynchronous_processing', '')}\n")
        drv: Dict[str, Any] = scal.get("data_retention_and_volume", {})
        if drv:
            lines.append("### Data Retention & Volume\n")
            lines.append(f"- **Volume Estimates:** {drv.get('volume_estimates', '')}")
            lines.append(f"- **Retention Policy:** {drv.get('retention_and_pruning_policy', '')}\n")

    # ── 5. Infrastructure View ───────────────────────────────────────────────
    infra: Dict[str, Any] = hld.get("5_infrastructure_view", {})
    if infra:
        lines.append("## 5. Infrastructure View\n")
        dt: Dict[str, Any] = infra.get("deployment_target", {})
        if dt:
            lines.append("### Deployment Target\n")
            lines.append(f"- **Hosting Platform:** {dt.get('hosting_platform', '')}")
            lines.append(f"- **Runtime Orchestration:** {dt.get('runtime_orchestration', '')}")
            lines.append(f"- **Environment Topology:** {dt.get('environment_topology', '')}\n")
        topo: List[Dict[str, Any]] = infra.get("topology_components") or []
        if topo:
            lines.append("### Topology Components\n")
            lines.append("| Component | Type | Technology | HA/Redundancy |")
            lines.append("|-----------|------|------------|----------------|")
            for t in topo:
                cn = (t.get("component_name") or "").replace("|", "\\|")
                it = (t.get("infrastructure_type") or "").replace("|", "\\|")
                tech = (t.get("technology") or "").replace("|", "\\|")
                ha = (t.get("redundancy_and_ha") or "").replace("|", "\\|")
                lines.append(f"| {cn} | {it} | {tech} | {ha} |")
            lines.append("")
        net: Dict[str, Any] = infra.get("networking_and_connectivity", {})
        if net:
            lines.append("### Networking & Connectivity\n")
            lines.append(f"- **Network Isolation:** {net.get('network_isolation', '')}")
            lines.append(f"- **Ingress/Egress Security:** {net.get('ingress_egress_security', '')}\n")
        rdr: Dict[str, Any] = infra.get("resilience_and_disaster_recovery", {})
        if rdr:
            lines.append("### Resilience & Disaster Recovery\n")
            lines.append(f"- **RTO:** {rdr.get('rto_target', '')}")
            lines.append(f"- **RPO:** {rdr.get('rpo_target', '')}")
            lines.append(f"- **Failover Mechanism:** {rdr.get('failover_mechanism', '')}\n")

    # ── Source Pages Mapping ─────────────────────────────────────────────────
    spm: List[Dict[str, Any]] = req.get("source_pages_mapping") or []
    if spm:
        lines.append("## Source Pages Mapping\n")
        lines.append("| Section | Source Titles |")
        lines.append("|---------|--------------|")
        for entry in spm:
            section = (entry.get("section") or "").replace("|", "\\|")
            titles = ", ".join(entry.get("source_titles") or []).replace("|", "\\|")
            lines.append(f"| {section} | {titles} |")
        lines.append("")

    return "\n".join(lines)


def _coerce_json(raw: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from an LLM response."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    if not raw:
        raise json.JSONDecodeError("LLM response was empty after removing markdown fences", raw, 0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        candidate = _extract_first_json_object(raw)
        if not candidate:
            raise
        return json.loads(candidate)


def _extract_first_json_object(raw: str) -> str:
    """Return the first balanced JSON object from a noisy LLM response."""
    start = raw.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(raw)):
        char = raw[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:idx + 1]
    return ""


def _chat_json_with_retry(
    llm,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    label: str,
) -> Dict[str, Any]:
    """Call the LLM and retry when it returns empty fences or malformed JSON."""
    raw = ""
    prompt = user_prompt
    for attempt in range(1, 4):
        raw = llm.chat(
            system_prompt=system_prompt,
            user_prompt=prompt,
            temperature=temperature if attempt == 1 else 0.0,
            max_tokens=max_tokens,
        )
        try:
            return _coerce_json(raw)
        except Exception as exc:  # noqa: BLE001
            preview = (raw or "").replace("\n", "\\n")[:300]
            print(
                f"[Requirements] {label} JSON parse attempt {attempt}/3 failed: {exc}. "
                f"Preview: {preview}"
            )
            if attempt == 3:
                raise RuntimeError(
                    f"LLM did not return valid JSON for {label}: {exc}\n---\n{raw[:1000]}"
                ) from exc
            prompt = "\n\n".join(
                [
                    user_prompt,
                    "CRITICAL RETRY INSTRUCTION:",
                    "Your previous response was not valid JSON. Return exactly one JSON object.",
                    "Do not use Markdown fences. Do not include explanations. Start with `{` and end with `}`.",
                    "If a field cannot be populated from evidence, use an empty string, empty list, or empty object matching the schema.",
                ]
            )

    raise RuntimeError(f"LLM did not return valid JSON for {label}")


def generate_requirements(
    *,
    product: Optional[str] = None,
    release: Optional[str] = None,
    n_results: int = 12,
    queries: Optional[List[Dict[str, str]]] = None,
    artifact_dir: Optional[str] = None,
    max_tokens: int = 12000,
    temperature: float = 0.1,
) -> RequirementsResult:
    """Run the full requirements-extraction pipeline.

    When using the default query suite (``queries=None``) the suite is split
    into three thematic batches, each producing its own focused LLM call:

    * **Batch 1** — Introduction (purpose/scope, definitions, references, context)
    * **Batch 2** — Logical view (modules, interfaces, interaction flows)
    * **Batch 3** — Non-functional (security, scalability, infrastructure)

    Each batch targets a dedicated partial JSON schema so the LLM can be fully
    exhaustive within its output-token budget.  The three partial responses are
    deep-merged into one unified requirements document before being persisted.

    Passing a custom ``queries`` list falls back to a single LLM call against
    the full schema.
    """
    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.utcnow().isoformat()

    context = artifact_context(product=product, release=release, create=True)
    vector_store = get_vector_store(project=context.product, release=context.release, create=False)
    retriever = EnhancedConfluenceRetriever(
        vector_store=vector_store,
        collection_name="confluence_pages",
        use_bm25=True,
        use_hybrid_search=True,
    )

    llm = get_llm_client()
    system_prompt = (
        "You are a senior system design engineer that converts raw Confluence documentation "
        "excerpts into a comprehensive, evidence-grounded JSON requirements document. "
        "Be exhaustive and detailed: prefer multi-sentence descriptions, preserve "
        "concrete numbers and identifiers, and split distinct capabilities into "
        "separate items. Never invent facts and never write filler like 'Not specified'. "
        "Output JSON only — no prose outside the JSON object."
    )

    all_evidence: List[EvidenceItem] = []

    if queries is None:
        # ── Three-batch mode (default) ──────────────────────────────────────
        # Each batch covers a distinct set of HLD sections so the model output
        # stays within token limits while remaining maximally detailed.
        partial_reqs: List[Dict[str, Any]] = []
        for part_queries, part_schema in zip(QUERY_SUITE_PARTS, _PARTIAL_SCHEMAS):
            part_evidence = _build_evidence(
                retriever, part_queries, n_results=n_results, product=context.product
            )
            all_evidence.extend(part_evidence)
            user_prompt = _build_llm_prompt(part_evidence, context.product, schema=part_schema)
            partial_req = _chat_json_with_retry(
                llm,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                label=f"requirements batch {len(partial_reqs) + 1}",
            )
            partial_reqs.append(partial_req)
        requirements = _merge_partial_requirements(partial_reqs)
    else:
        # ── Single-call mode (custom queries) ───────────────────────────────
        all_evidence = _build_evidence(
            retriever, queries, n_results=n_results, product=context.product
        )
        user_prompt = _build_llm_prompt(all_evidence, context.product)
        requirements = _chat_json_with_retry(
            llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            label="requirements",
        )

    completed_at = datetime.utcnow().isoformat()

    # ── Persist artifacts ────────────────────────────────────────────────────
    out_dir = artifact_dir or str(context.stage_dir("hld"))
    os.makedirs(out_dir, exist_ok=True)
    timestamped_artifact_path = os.path.join(out_dir, f"requirements_{context.timestamp}.json")
    payload = {
        "job_id": job_id,
        "product": context.product,
        "release": context.release,
        "timestamp": context.timestamp,
        "started_at": started_at,
        "completed_at": completed_at,
        "llm": llm.info(),
        "requirements": requirements,
        "evidence": [
            {
                "query_key": e.query_key,
                "query": e.query,
                "sources": e.sources,
            }
            for e in all_evidence
        ],
    }
    with open(timestamped_artifact_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    return RequirementsResult(
        job_id=job_id,
        product=context.product,
        release=context.release,
        started_at=started_at,
        completed_at=completed_at,
        requirements=requirements,
        evidence=all_evidence,
        artifact_path=timestamped_artifact_path,
    )
