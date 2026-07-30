Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: configuring-certificate-authority-with-openssl

Description: A Certificate Authority (CA) is the trust anchor in a PKI hierarchy,

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## When to Use
- When deploying or configuring configuring certificate authority with openssl capabilities in your environment
- When establishing security controls aligned to compliance requirements
- When building or improving security architecture for this domain
- When conducting security assessments that require this implementation

## Objectives
- Create a Root CA with self-signed certificate
- Create an Intermediate CA signed by the Root CA
- Issue server and client certificates from the Intermediate CA
- Configure Certificate Revocation Lists (CRLs)
- Implement certificate policies and constraints
- Build a complete PKI hierarchy programmatically

## Security Considerations
- Root CA private key must be stored offline (air-gapped HSM)
- Use minimum 4096-bit RSA or P-384 ECDSA for CA keys
- Set path length constraints on intermediate CAs
- Implement certificate policies (OIDs)
- Enable CRL and OCSP for revocation checking
- Audit all certificate issuance operations

## Overview
A Certificate Authority (CA) is the trust anchor in a PKI hierarchy, responsible for issuing, signing, and revoking digital certificates. This skill covers building a two-tier CA hierarchy (Root CA + Intermediate CA) using OpenSSL and the Python cryptography library, including CRL distribution, OCSP responder configuration, and certificate policy management.

## Validation Criteria
- [ ] Root CA self-signed certificate is valid
- [ ] Intermediate CA certificate chains to Root CA
- [ ] Issued certificates chain to Intermediate -> Root
- [ ] Path length constraints are enforced
- [ ] CRL is generated and accessible
- [ ] Revoked certificates appear in CRL
- [ ] Certificate policies are correctly embedded

## Prerequisites
- Familiarity with cryptography concepts and tools
- Access to a test or lab environment for safe execution
- Python 3.8+ with required dependencies installed
- Appropriate authorization for any testing activities

## Certificate Extensions
| Extension | Purpose | Critical |
|-----------|---------|----------|
| basicConstraints | CA:TRUE/FALSE, pathLenConstraint | Yes |
| keyUsage | keyCertSign, cRLSign, digit
