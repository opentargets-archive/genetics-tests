#! /usr/bin/env python

"""

Copyright [1999-2015] Wellcome Trust Sanger Institute and the EMBL-European Bioinformatics Institute

Licensed under the Apache License, Version 2.0 (the "License")
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

		 http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

"""

	Please email comments or questions to the public Ensembl
	developers list at <http://lists.ensembl.org/mailman/listinfo/dev>.

	Questions may also be sent to the Ensembl help desk at
	<http://www.ensembl.org/Help/Contact>.

"""

import sys
import argparse
import re
import requests
import collections
import xmltodict
import pybedtools
import math
import json
import cPickle as pickle

# Class definitions
SNP = collections.namedtuple("SNP", ['rsID', 'chrom', 'pos'])
Gene = collections.namedtuple("Gene", ['name', 'id', 'chrom', 'tss', 'biotype'])
Disease = collections.namedtuple('Disease', ['name', 'efo'])
GWAS_Association = collections.namedtuple('GWAS_Association', ['snp','disease','efo','pvalue','source','study'])
GWAS_SNP = collections.namedtuple('GWAS_SNP', ['snp','disease','efo','pvalue', 'evidence'])
GWAS_Cluster = collections.namedtuple('GWAS_Cluster', ['gwas_snps','ld_snps'])
Cisregulatory_Evidence = collections.namedtuple('Cisregulatory_Evidence', ['snp','gene','score','source','study','tissue'])
Regulatory_Evidence = collections.namedtuple('Regulatory_Evidence', ['snp','score','source','study','tissue'])
GeneSNP_Association = collections.namedtuple('GeneSNP_Association', ['gene', 'snp', 'score', 'cisregulatory_evidence', 'regulatory_evidence'])
GeneCluster_Association = collections.namedtuple('GeneCluster_Association', ['gene', 'cluster', 'score', 'evidence'])
FDR_Model = collections.namedtuple('FDR_Model', ['FDR','BIN_WIDTH','MAX_DISTANCE'])

# Bit and bobs
phenotype_cache = ()
VEP_impact_to_score = {
	'HIGH': 4,
	'MEDIUM': 3,
	'LOW': 2,
	'MODIFIER': 1,
	'MODERATE': 1
}

NCBI_Taxon_ID = {
	'Human': 9606
}

# Globals
DATABASES_DIR = None
SPECIES = None
DEBUG = True
PVALUE_CUTOFF = 1e-4

'''
Development TODO list:

A. Datasets to integrate:
	-Cisregulatory annotations:
		PCHIC (STOPGAP Scoring: Single cell line: +1, multiple cell lines: +2)

	-Epigenetic activity:
		PhyloP (STOPGAP Scoring: FPR 0-0.6: +2, 0.6-0.85: +1,0.85-1: +0)
		DHS
		Fantom5

B. Code improvements:
	Pathways analysis (Downstream)
	Take into account population composition in LD calcs

C. Model improvements:
	Replace PICS with Bayesian model
	Fine mapping of summary data
	Tissue selection

'''

def main():
	"""

		Reads commandline parameters, prints corresponding associated genes with evidence info

	"""
	options = get_options()
	print json.dumps(diseases_to_genes(options.diseases, options.efos, options.populations, options.tissues))

def get_options():
	"""

		Reads commandline parameters
		Returntype: 
			{
				diseases: [ string ],
				populations: [ string ],
				tissues: [ string ],
			}

	"""
	parser = argparse.ArgumentParser(description='Search GWAS/Regulatory/Cis-regulatory databases for causal genes')
	parser.add_argument('--efos', nargs='*')
	parser.add_argument('--diseases', nargs='*')
	parser.add_argument('--populations', nargs='*', default=['1000GENOMES:phase_3:GBR'])
	parser.add_argument('--tissues', nargs='*')
	parser.add_argument('--species', nargs='*', default = 'Human')
	parser.add_argument('--database_dir', dest = 'databases', default = 'databases')
	parser.add_argument('--debug', '-g', action = 'store_true')
	options = parser.parse_args()

	global DATABASES_DIR
	DATABASES_DIR = options.databases
	global SPECIES
	SPECIES = options.species
	global DEBUG
	DEBUG = DEBUG or options.debug

	assert DATABASES_DIR is not None
	assert options.efos is not None or options.diseases is not None

	if options.diseases is None:
		options.diseases = []

	if options.efos is None:
		options.efos = filter(lambda X: X is not None, (efo_suggest(disease) for disease in options.diseases))

	# Expand list of EFOs to children, concatenate, remove duplicates
	options.efos = concatenate(map(efo_children, options.efos))

	return options

def efo_suggest(term):
	"""
		
		Find most appropriate EFO term for arbitrary string
		Arg:
		* string
		Returntype: string (EFO ID)

	"""
	server = 'http://www.ebi.ac.uk/spot/zooma/v2/api'
	url_term = re.sub(" ", "%20", term)
	ext = "/summaries/search?query=%s" % (url_term)
	hash = get_rest_json(server, ext)
	'''

		Example search result:
		{
			status: "/api/status/ok",
			result: [
				{
					'mid': '3804279AF8462F3A01EAEE2589C94781F248F9D7',
					'notable': {
						'name': 'disease; EFO_0000311',
						'id': 'summary_from_disease_to_EFO_0000311'
					},
					'name': 'cancer',
					'score': '86.11212'
				}
			]	
		}

	'''
	result = hash['result']
	hits = filter(lambda X: re.search('EFO_\d+' , X['notable']['name']), result)
	sorted_hits = sorted(hits, key = lambda X: X['score'])
	if len(hits):
		selection = sorted_hits[-1]['notable']['name']
		efo = re.sub('.*\(EFO_[0-9]*\)$', '\1', selection)
		if DEBUG:
			print "Suggested EFO %s" % efo
		return efo

	else:
		if DEBUG:
			print "Suggested EFO N/A"
		return None

def efo_children(efo):
	"""

		Return list of children EFO IDs
		Arg:
		* string (EFO ID)
		Returntype: [ string ] (EFI IDs)

	"""
	server = 'http://www.ebi.ac.uk'
	page = 1
	res = [efo]

	while (True):
		ext = "/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252F%s/descendants?page%i&size=10" % (efo, page)
		hash = get_rest_json(server, ext)
		'''

			OWL Output format:
			{
				"_links" : {
					"first" : {
			"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_0001071/descendants?page=0&size=10"
					},
					"prev" : {
			"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_0001071/descendants?page=0&size=10"
					},
					"self" : {
			"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_0001071/descendants?page=1&size=10"
					},
					"last" : {
			"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_0001071/descendants?page=1&size=10"
					}
				},
				"_embedded" : {
					"terms" : [ {
						"iri" : "http://www.ebi.ac.uk/efo/EFO_1000333",
						"label" : "Lung Inflammatory Myofibroblastic Tumor",
						"description" : [ "An intermediate fibroblastic neoplasm arising from the lung. It is characterized by the presence of spindle-shaped fibroblasts and myofibroblasts, and a chronic inflammatory infiltrate composed of eosinophils, lymphocytes and plasma cells." ],
						"annotation" : {
							"NCI_Thesaurus_definition_citation" : [ "NCIt:C39740" ]
						},
						"synonyms" : null,
						"ontology_name" : "efo",
						"ontology_prefix" : "EFO",
						"ontology_iri" : "http://www.ebi.ac.uk/efo",
						"is_obsolete" : false,
						"is_defining_ontology" : true,
						"has_children" : false,
						"is_root" : false,
						"short_form" : "EFO_1000333",
						"obo_id" : "EFO:1000333",
						"_links" : {
							"self" : {
								"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_1000333"
							},
							"parents" : {
								"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_1000333/parents"
							},
							"ancestors" : {
								"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_1000333/ancestors"
							},
							"jstree" : {
								"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_1000333/jstree"
							},
							"graph" : {
								"href" : "http://www.ebi.ac.uk/ols/beta/api/ontologies/efo/terms/http253A252F252Fwww.ebi.ac.uk252Fefo252FEFO_1000333/graph"
							}
						}
					} ]
				},
				"page" : {
					"size" : 10,
					"totalElements" : 20,
					"totalPages" : 2,
					"number" : 1
				}
			}

		'''

		if '_embedded' in hash:
			res += [ result['short_form'] for result in hash['_embedded']['terms'] ]

		if page > int(hash['page']['totalPages']):
			break

		page += 1
	
	if DEBUG:
		print "EFO children: " + "\t".join(res)
	return res

def diseases_to_genes(diseases, efos, populations, tissues):
	"""

		Associates genes from a list of diseases
		Args: 
		* [ string ] (trait descriptions - free strings)
		* [ string ] (trait EFO identifiers)
		* [ string ] (population names)
		* [ string ] (tissue names)
		Returntype: [ GeneCluster_Association ]

	"""
	res = gwas_snps_to_genes(diseases_to_gwas_snps(diseases, efos), populations, tissues)

	for candidate in res:
		candidate['MeSH'] = gene_to_MeSH(candidate.gene)
		candidate['gene_phenotype_association'] = gene_to_phenotypes(candidate.gene)

	return res

def diseases_to_gwas_snps(diseases, efos):
	"""

		Associates gwas_snps from a list of diseases
		Args: 
		* [ string ] (trait descriptions - free strings )
		* [ string ] (trait EFO identifiers)
		Returntype: [ Association_SNP ]

	"""
	res = filter(lambda X: X.pvalue < PVALUE_CUTOFF, scan_disease_databases(diseases, efos))

	if DEBUG:
		print "Found %i GWAS SNPs associated to diseases (%s) or EFO IDs (%s) after p-value filter (%f)" % (len(res), ", ".join(diseases), ", ".join(efos), PVALUE_CUTOFF)

	return res

def scan_disease_databases(diseases, efos):
	"""

		Associates gwas_snps from a list of diseases
		Args: 
		* [ string ] (trait descriptions)
		* [ string ] (trait EFO identifiers)
		Returntype: [ GWAS_SNP ]

	"""
	if DEBUG:
		print "Searching for GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in all databases" % (", ".join(diseases), ", ".join(efos))

	hits = concatenate(function(diseases, efos) for function in database_functions)

	associations_by_snp = dict()
	for hit in hits:
		if hit.snp in associations_by_snp:
			record = associations_by_snp[hit.snp]
			record.evidence.append(hit)
			if record.pvalue > hit.pvalue:
				associations_by_snp[hit.snp] = GWAS_SNP(
					snp = record.snp,
					pvalue = hit.pvalue,
					disease = record.disease,
					efo = record.efo,
					evidence = record.evidence
				)
		else:
			associations_by_snp[hit.snp] = GWAS_SNP(
				snp = hit.snp,
				pvalue = hit.pvalue,
				disease = hit.disease,
				efo = hit.efo,
				evidence = [ hit ]
			)

	res = associations_by_snp.values()

	if DEBUG:
		print "Found %i unique GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in all databases" % (len(res), ", ".join(diseases), ", ".join(efos))

	return res

def GWASCatalog(diseases, efos):
	"""

		Returns all GWAS SNPs associated to a disease in GWAS Catalog
		Args:
		* [ string ] (trait descriptions)
		* [ string ] (trait EFO identifiers)
		Returntype: [ GWAS_Association ]

	"""
	file = open(DATABASES_DIR+"/GWAS_Catalog.txt")
	res = concatenate(get_gwas_catalog_association(line, diseases, efos) for line in file)

	if DEBUG:
		print "\tFound %i GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in GWAS Catalog" % (len(res), ", ".join(diseases), ", ".join(efos))

	return res

def get_gwas_catalog_association(line, diseases, efos):
	'''

		GWAS Catalog flat file format (waiting for REST API...)

		1.	DATE ADDED TO CATALOG: Date added to catalog
		2.	PUBMEDID: PubMed identification number
		3.	FIRST AUTHOR: Last name of first author
		4.	DATE: Publication date (online (epub) date if available)
		5.	JOURNAL: Abbreviated journal name
		6.	LINK: PubMed URL
		7.	STUDY: Title of paper (linked to PubMed abstract)
		8.	DISEASE/TRAIT: Disease or trait examined in study
		9.	INITIAL SAMPLE DESCRIPTION: Sample size for Stage 1 of GWAS
		10.	REPLICATION SAMPLE DESCRIPTION: Sample size for subsequent replication(s)
		11.	REGION: Cytogenetic region associated with rs number (NCBI)
		12.	CHR_ID: Chromosome number associated with rs number (NCBI)
		13.	CHR_POS: Chromosomal position associated with rs number (dbSNP Build 144, Genome Assembly GRCh38.p2, NCBI)
		14.	REPORTED GENE (S): Gene(s) reported by author
		15.	MAPPED GENE(S): Gene(s) mapped to the strongest SNP (NCBI). If the SNP is located within a gene, that gene is listed. If the SNP is intergenic, the upstream and downstream genes are listed, separated by a hyphen.
		16.	UPSTREAM_GENE_ID: Entrez Gene ID for nearest upstream gene to rs number, if not within gene (NCBI)
		17.	DOWNSTREAM_GENE_ID: Entrez Gene ID for nearest downstream gene to rs number, if not within gene (NCBI)
		18.	SNP_GENE_IDS: Entrez Gene ID, if rs number within gene; multiple genes denotes overlapping transcripts (NCBI)
		19.	UPSTREAM_GENE_DISTANCE: distance in kb for nearest upstream gene to rs number, if not within gene (NCBI)
		20.	DOWNSTREAM_GENE_DISTANCE: distance in kb for nearest downstream gene to rs number, if not within gene (NCBI)
		21.	STRONGEST SNP-RISK ALLELE: SNP(s) most strongly associated with trait + risk allele (? for unknown risk allele). May also refer to a haplotype.
		22.	SNPS: Strongest SNP; if a haplotype is reported above, may include more than one rs number (multiple SNPs comprising the haplotype)
		23.	MERGED: denotes whether the SNP has been merged into a subsequent rs record (0 = no; 1 = yes; NCBI) SNP_ID_CURRENT: current rs number (will differ from strongest SNP when merged = 1)
		24.	[Inserted] SNP_ID_CURRENT
		25.	CONTEXT: SNP functional class (NCBI)
		26.	INTERGENIC: denotes whether SNP is in intergenic region (0 = no; 1 = yes; NCBI)
		27.	RISK ALLELE FREQUENCY: Reported risk allele frequency associated with strongest SNP
		28.	P-VALUE: Reported p-value for strongest SNP risk allele (linked to dbGaP Association Browser)
		29.	PVALUE_MLOG: -log(p-value)
		30.	P-VALUE (TEXT): Information describing context of p-value (e.g. females, smokers). Note that p-values are rounded to 1 significant digit (for example, a published pvalue of 4.8 x 10-7 is rounded to 5 x 10-7).
		31.	OR or BETA: Reported odds ratio or beta-coefficient associated with strongest SNP risk allele
		32.	95% CI (TEXT): Reported 95% confidence interval associated with strongest SNP risk allele
		33.	PLATFORM (SNPS PASSING QC): Genotyping platform manufacturer used in Stage 1; also includes notation of pooled DNA study design or imputation of SNPs, where applicable
		34.	[Inserted] CNV
		35.	[Deleted] MAPPED_TRAIT: Mapped Experimental Factor Ontology trait for this study
		36.	MAPPED_TRAIT_URI: URI of the EFO trait

	'''
	items = line.rstrip().split('\t')
	if len(items) != 36:
		print line
		for elem in enumerate(items):
			print "\t".join(map(str, elem))
		assert False, line

	my_efos = re.sub("http://www.ebi.ac.uk/efo/", "", items[35]).split(", ")
	if items[7] in diseases or any(my_efo in efos for my_efo in my_efos):
		return [ 
			GWAS_Association(
				pvalue = float(items[27]),
				snp = snp,
				disease = items[7],
				efo = items[35],
				source = 'GWAS Catalog',
				study = None
			)
			for snp in items[21].split(',')
		]
	else:
		return []

def GRASP(diseases, efos):
	"""

		Returns all GWAS SNPs associated to a disease in GRASP
		Args:
		* [ string ] (trait descriptions)
		* [ string ] (trait EFO identifiers)
		Returntype: [ GWAS_Association ]

	"""
	file = open(DATABASES_DIR+"/GRASP.txt")
	res = [ get_grasp_association(line, diseases, efos) for line in file ]
	res = filter(lambda X: X is not None, res)

	if DEBUG:
		print "\tFound %i GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in GRASP" % (len(res), ", ".join(diseases), ", ".join(efos))

	return res

def get_grasp_association(line, diseases, efos):
	'''

		GRASP file format:
		1. NHLBIkey
		2. HUPfield
		3. LastCurationDate
		4. CreationDate
		5. SNPid(dbSNP134)
		6. chr(hg19)
		7. pos(hg19)
		8. PMID
		9. SNPid(in paper)
		10. LocationWithinPaper
		11. Pvalue
		12. Phenotype
		13. PaperPhenotypeDescription
		14. PaperPhenotypeCategories
		15. DatePub
		16. InNHGRIcat(as of 3/31/12)
		17. Journal
		18. Title
		19. IncludesMale/Female Only Analyses
		20. Exclusively Male/Female
		21. Initial Sample Description
		22. Replication Sample Description
		23. Platform [SNPs passing QC]
		24. GWASancestryDescription
		25. TotalSamples(discovery+replication)
		26. TotalDiscoverySamples
		27. European Discovery
		28. African Discovery
		29. East Asian Discovery
		30. Indian/South Asian Discovery
		31. Hispanic Discovery
		32. Native Discovery
		33. Micronesian Discovery
		34. Arab/ME Discovery
		35. Mixed Discovery
		36. Unspecified Discovery
		37. Filipino Discovery
		38. Indonesian Discovery
		39. Total replication samples
		40. European Replication
		41. African Replication
		42. East Asian Replication
		43. Indian/South Asian Replication
		44. Hispanic Replication
		45. Native Replication
		46. Micronesian Replication
		47. Arab/ME Replication
		48. Mixed Replication
		49. Unspecified Replication
		50. Filipino Replication
		51. Indonesian Replication
		52. InGene
		53. NearestGene
		54. InLincRNA
		55. InMiRNA
		56. InMiRNABS
		57. dbSNPfxn
		58. dbSNPMAF
		59. dbSNPalleles/het/se
		60. dbSNPvalidation
		XX61. dbSNPClinStatus
		XX62. ORegAnno
		XX63. ConservPredTFBS
		XX64. HumanEnhancer
		XX65. RNAedit
		XX66. PolyPhen2
		XX67. SIFT
		XX68. LS-SNP
		XX69. UniProt
		XX70. EqtlMethMetabStudy
		71. EFO string 

	'''
	items = line.rstrip().split('\t')
	if len(items) < 61:
		assert False, line
	if items[11] in diseases or items[60] in efos:
		return GWAS_Association(
			pvalue = float(items[10]),
			snp = "rs" + items[4],
			disease = items[11],
			efo = items[70],
			source = "GRASP",
			study = items[7]
		)
	else:
		return None

def Phewas_Catalog(diseases, efos):
	"""

		Returns all GWAS SNPs associated to a disease in PhewasCatalog
		Args:
		* [ string ] (trait descriptions)
		* [ string ] (trait EFO identifiers)
		Returntype: [ GWAS_Association ]

	"""
	file = open(DATABASES_DIR+"/Phewas_Catalog.txt")
	res = [ get_phewas_catalog_association(line, diseases, efos) for line in file ]
	res = filter(lambda X: X is not None, res)

	if DEBUG:
		print "\tFound %i GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in Phewas Catalog" % (len(res), ", ".join(diseases), ", ".join(efos))

	return res

def get_phewas_catalog_association(line, diseases, efos):
	'''

		Phewas Catalog format:
		1. chromosome
		2. snp
		3. phewas phenotype
		4. cases
		5. p-value
		6. odds-ratio
		7. gene_name
		8. phewas code
		9. gwas-associations
		10. [Inserte] EFO identifier (or N/A)
		
	'''
	items = line.rstrip().split('\t')
	if items[2] in diseases or items[9] in efos:
		return GWAS_Association (
			pvalue = float(items[4]),
			snp = items[1],
			disease = items[3],
			efo = items[9],
			source = "Phewas Catalog",
			study = None
		)
	else:
		return None

def GWAS_DB(diseases, efos):
	"""

		Returns all GWAS SNPs associated to a disease in GWAS_DB
		Args:
		* [ string ] (trait descriptions)
		* [ string ] (trait EFO identifiers)
		Returntype: [ GWAS_Association ]

	"""
	file = open(DATABASES_DIR+"/GWAS_DB.txt")

	# Note the format of the EFO strings is modified in this file format, so we need to change the queries
	efos2 = [re.sub("_", "ID:", efo) for efo in efos]
	res = [ get_gwas_db_association(line, diseases, efos) for line in file ]
	res = filter(lambda X: X is not None, res)

	if DEBUG:
		print "\tFound %i GWAS SNPs associated to diseases (%s) or EFO IDs (%s) in GWAS DB" % (len(res), ", ".join(diseases), ", ".join(efos))

	return res

def get_gwas_db_association(line, diseases, efos):
	'''

		GWAS DB data
		1. CHR
		2. POS
		3. SNPID
		4. REF
		5. ALT
		6. ORI_SNPID
		7. PMID
		8. P_VALUE
		9. P_VALUE_TEXT
		10. OR/BETA
		11. CI95_TEXT
		12. GWAS_INITIAL_SAMPLE_SIZE
		13. SUB_POPULATION
		14. SUPER_POPULATION
		15. GWAS_TRAIT
		16. HPO_ID
		17. HPO_TERM
		18. DO_ID
		19. DO_TERM
		20. MESH_ID
		21. MESH_TERM
		22. EFO_ID
		23. EFO_TERM
		24. DOLITE_TERM
		25. RISK_ALLELE
		26. PUBLICATION_TYPE
		27. AA
		28. GENE_SYMBOL
		29. TYPE
		30. REFGENE

	'''
	items = line.rstrip().split('\t')
	if items[14] in diseases or items[21] in efos:
		return GWAS_Association(
			pvalue = float(items[7]),
			snp = items[2],
			disease = items[14],
			efo = items[21],
			source = "GWAS DB",
			study = items[6]
		)
	else:
		return None

def gwas_snps_to_genes(gwas_snps, populations, tissue_weights):
	"""

		Associates Genes to gwas_snps of interest
		Args: 
		* [ GWAS_Association ]
		* { population_name: scalar (weight) }
		* { tissue_name: scalar (weight) }
		Returntype: [ GeneCluster_Association ]

	"""
	# Must set the tissue settings before separating out the gwas_snps
	if tissue_weights is None:
		tissue_weights = gwas_snps_to_tissue_weights(gwas_snps)

	clusters = cluster_gwas_snps(gwas_snps, populations)
	res = concatenate(cluster_to_genes(cluster, tissue_weights, populations) for cluster in clusters)

	if DEBUG:
		print "\tFound %i genes associated to all clusters" % (len(res))

	return [ sorted(res, key=lambda X: X.score)[-1] ]

def gwas_snps_to_tissue_weights(gwas_snps):
	"""

		Associates list of tissues to list of gwas_snps
		Args: 
		* [ GWAS_SNP ]
		Returntype: [ string ]

	"""
	return ['Whole_Blood'] # See FORGE??

def cluster_gwas_snps(gwas_snps, populations):
	"""

		Bundle together gwas_snps within LD threshold
		* [ GWAS_SNP ]
		* [ Bio::Ensembl::Variation::Population ]
		Returntype: [ GWAS_Cluster ]

	"""
	gwas_snp_locations = concatenate(get_gwas_snp_locations(snp) for snp in gwas_snps)

	if DEBUG:
		"Found %i locations from %i GWAS SNPs" % (len(gwas_snp_locations), len(gwas_snps))

	preclusters = filter (lambda X: X is not None, [ gwas_snp_to_precluster(gwas_snp_location, populations) for gwas_snp_location in gwas_snp_locations ])
	clusters = merge_preclusters(preclusters)

	if DEBUG:
		"Found %i clusters from %i GWAS SNP locations" % (len(clusters), len(gwas_snp_locations))

	return clusters


def gwas_snp_to_precluster(gwas_snp, populations):
	"""

		Extract neighbourhood of GWAS snp
		Args:
		* [ GWAS_SNP ]
		* [ string ] (populations)
		Returntype: GWAS_Cluster
		
	"""
	# Get all LD values around SNP
	rsID = gwas_snp.snp.rsID
	server = 'http://rest.ensembl.org'
	ext = '/ld/%s/%s?content-type=application/json;population_name=%s;r2=%f' % (SPECIES, rsID, populations[0], 0.5) # TODO Mixed population model
	try:
		lds = get_rest_json(server, ext)
	except:
		return None

	'''

		Example format:

		[
			{
				"variation1": "rs3774356",
				"population_name": "1000GENOMES:phase_3:KHV",
				"variation2": "rs1042779",
				"r2": "0.153806",
				"d_prime": "0.881692"
			}
		]

	'''

	# Reduce to list of linked SNPs
	ld_snps = [ ld['variation2'] for ld in lds if ld['variation1'] == rsID ] \
	        + [ ld['variation1'] for ld in lds if ld['variation2'] == rsID ]

	# Get locations
	mapped_ld_snps = concatenate(get_snp_locations(snp) for snp in ld_snps) + [ gwas_snp.snp ]

	# Note: Ensembl REST server imposes a 25kb limit, whereas STOPGAP imposes a looser 500kb limit
	
	return GWAS_Cluster(
		gwas_snps = [ gwas_snp ],
		ld_snps = mapped_ld_snps
	)

def get_gwas_snp_locations(gwas_snp):
	"""

		Extract locations of GWAS SNP:
		Args
		* GWAS_SNP
		Returntype: [ GWAS_SNP ]

	"""
	return [ 
		GWAS_SNP(
			snp = mapped_snp,
			disease = gwas_snp.disease,
			efo = gwas_snp.efo,
			pvalue = gwas_snp.pvalue,
			evidence = gwas_snp.evidence
		) 
		for mapped_snp in get_snp_locations(gwas_snp.snp)
	]


def merge_preclusters(preclusters):
	"""

		Bundle together preclusters that share one LD snp
		* [ Cluster ]
		Returntype: [ Cluster ]

	"""
	snp_owner = dict()
	kill_list = list()
	for cluster in preclusters:
		for ld_snp in cluster.ld_snps:
			if ld_snp in snp_owner and snp_owner[ld_snp] is not cluster:
				other_cluster = snp_owner[ld_snp]
				print "Overlap between %i and %i" % (id(cluster), id(other_cluster))

				# Merge data from current cluster into previous cluster
				merged_gwas_snps = other_cluster.gwas_snps + cluster.gwas_snps
				merged_ld_snps = dict((ld_snp.rsID, ld_snp) for ld_snp in cluster.ld_snps + other_cluster.ld_snps).values()
				snp_owner[ld_snp] = GWAS_Cluster(
					gwas_snps = merged_gwas_snps,
					ld_snps = merged_ld_snps
				)

				# Mark for deletion
				kill_list.append(cluster)
				for snp in cluster.ld_snps:
					snp_owner[snp] = other_cluster

				# Exit from that cluster
				break
			else:
				snp_owner[ld_snp] = cluster
 
	res = filter(lambda cluster: cluster not in kill_list, preclusters)

	if DEBUG:
		print "\tFound %i clusters from the GWAS peaks" % (len(res))

	return res


def cluster_to_genes(cluster, tissues, populations):
	"""

		Associated Genes to a cluster of gwas_snps
		Args: 
		* [ Cluster ]
		* { tissue_name: scalar (weights) }
		* { population_name: scalar (weight) }
		Returntype: [ GeneCluster_Association ]

	"""
	# Obtain interaction data from LD snps
	associations = ld_snps_to_genes(cluster.ld_snps, tissues)

	# Compute LD based scores
	top_gwas_hit = sorted(cluster.gwas_snps, key=lambda X: X.pvalue)[-1] 
	ld = get_lds_from_top_gwas(top_gwas_hit.snp, cluster.ld_snps, populations)
	pics = PICS(ld, top_gwas_hit.pvalue)

	gene_scores = dict(
		((association.gene, association.snp), (association, association.score * ld[association.snp])) 
		for association in associations if association.snp in ld
	)

	if len(gene_scores) == 0:
		return []
	
	# OMIM exception
	max_score = max(X[1] for X in gene_scores.values())
	for gene, snp in gene_scores:
		if len(gene_to_phenotypes(gene)):
			gene_scores[(gene, snp)][1] = max_score


	res = [
		GeneCluster_Association(
			gene = gene,
			score = total_score(pics[snp], gene_scores[(gene, snp)][1]), 
			cluster = cluster,
			evidence = gene_scores[(gene, snp)][0]
		)
		for (gene, snp) in gene_scores if snp in pics
	]

	if DEBUG:
		print "\tFound %i genes associated around gwas_snp %s" % (len(res), top_gwas_hit.snp)

	# Pick the association with the highest score
	return [ sorted(res, key=lambda X: X.score)[-1] ]

def get_lds_from_top_gwas(gwas_snp, ld_snps, populations):
	"""

		Compute LD between top GWAS hit and all LD snps in list.
		Args:
		* SNP
		* [ SNP ]
		Returntype: { rsId: scalar (ld) }

	"""
	server = "http://rest.ensembl.org"
	ext = "/ld/%s/%s?type=application/json;population_name=%s;r2=%f;" % (SPECIES, gwas_snp.rsID, populations[0], 0.5) # TODO mixed-population model

	try:
		lds = get_rest_json(server, ext)
	except:
		return dict()

	rsID_to_SNPs = dict( (snp.rsID, snp) for snp in ld_snps)
	return dict(
		(rsID_to_SNPs[ld['variation1']], float(ld['r2'])) 
		for ld in lds if ld['variation1'] in rsID_to_SNPs
	)

def PICS(ld, pvalue):
	"""

		PICS score presented in http://pubs.broadinstitute.org/pubs/finemapping/
		Args: 
		* { SNP: scalar } (LD)
		* scalar (pvalue)
		Returntype: { rsID: scalar } (PICS score)

	"""
	minus_log_pvalue = - math.log(pvalue) / math.log(10); 
	SD = dict()
	Mean = dict()
	prob = dict()
	sum = 0

	for snp in ld.keys():
		if snp in ld:
			# Calculate the standard deviation of the association signal at the SNP 
			SD[snp] = math.sqrt(1 - math.sqrt(ld[snp]) ** 6.4) * math.sqrt(minus_log_pvalue) / 2; 

			# calculate the expected mean of the association signal at the SNP 
			Mean[snp] = ld[snp] * minus_log_pvalue; 
		else:
			# Defaults for remote SNPs
			SD[snp] = 0
			Mean[snp] = 1 + minus_log_pvalue

		# Calculate the probability of each SNP
		if SD[snp]:
			prob[snp] = 1 - pnorm(minus_log_pvalue, Mean[snp], SD[snp])
		else:
			prob[snp] = 1

		# Normalisation sum
		sum += prob[snp]

	# Normalize the probabilies so that their sum is 1.
	return dict((snp, prob[snp] / sum) for snp in prob.keys())

def pnorm(x, mu, sd):
	"""

		Normal distribution PDF
		Args:
		* scalar: variable
		* scalar: mean
		* scalar: standard deviation
		Return type: scalar (probability density)

	"""
	return math.exp ( - ((x - mu) / sd) ** 2 / 2 ) / (sd * 2.5)

def total_score(pics, gene_score):
	"""

		Computes a weird mean function from ld_snp PICs score and Gene/SNP association score
		Args: 
		* PICS: scalar
		* gene_score: scalar
		Returntype: scalar

	"""
	A = pics * (pics ** (1/3))
	B = gene_score * (gene_score ** (1/3))
	return ((A + B) / 2) ** 3

def ld_snps_to_genes(ld_snps, tissues):
	"""

		Associates genes to LD linked SNP
		Args: 
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: [ GeneSNP_Association ] 

	"""
	# Search for SNP-Gene pairs:
	cisreg = cisregulatory_evidence(ld_snps, tissues)

	# Extract list of relevant SNPs:
	selected_snps = set(gene_snp_pair[1] for gene_snp_pair in cisreg)

	# Extract SNP specific info:
	reg = regulatory_evidence(selected_snps, tissues)

	return [
		GeneSNP_Association(
			gene = gene,
			snp = snp,
			cisregulatory_evidence = cisreg[(gene, snp)],
			regulatory_evidence = reg[snp],
			score = sum(float(evidence.score) for evidence in reg[snp] + cisreg[(gene, snp)])
		)
		for (gene, snp) in cisreg
	]

def cisregulatory_evidence(ld_snps, tissues):
	"""

		Associates genes to LD linked SNP
		Args: 
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: { (Gene, SNP): Cisregulatory_Evidence } 

	"""
	if DEBUG:
		print "Searching for cis-regulatory data on %i SNPs in all databases" % (len(ld_snps))
	evidence = concatenate(function(ld_snps, tissues) for function in ld_snp_to_gene_functions)

	filtered_evidence = filter(lambda association: association.gene.biotype != "protein_coding", evidence)

	# Group by (gene,snp) pair:
	res = collections.defaultdict(list)
	for association in filtered_evidence:
		res[(association.gene, association.snp)] += evidence

	if DEBUG:
		print "Found %i cis-regulatory interactions in all databases" % (len(res))

	return res
 
def GTEx(ld_snps, tissues):
	"""

		Returns all genes associated to a set of SNPs in GTEx 
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: [ Cisregulatory_Evidence ]

	"""
	# Find all genes with 1Mb
	start = min(snp.pos for snp in ld_snps)
	end = max(snp.pos for snp in ld_snps)
	chrom = ld_snps[0].chrom

	server = 'http://rest.ensembl.org'
	ext = '/overlap/region/%s/%s:%i-%i?feature=gene;content-type=application/json' % (SPECIES, chrom, start - 1e6, end + 1e6)
	genes = [ Gene(
			name = gene['external_name'],
			id = gene['id'],
			chrom = gene['seq_region_name'],
			tss = int(gene['start']) if gene['strand'] > 0 else int(gene['end']),
			biotype = gene['biotype']
		)
		for gene in get_rest_json(server, ext)
	]
	snp_hash = dict( (snp.rsID, snp) for snp in ld_snps)
	res = concatenate((GTEx_gene(gene, tissues, snp_hash) for gene in genes))

	if DEBUG:
		print "\tFound %i interactions in GTEx" % (len(res))

	return res

def GTEx_gene(gene, tissues, snp_hash):
	"""

		Returns all SNPs associated to a gene in GTEx 
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		* { rsID: rsID }
		Returntype: [ Cisregulatory_Evidence ]

	"""
	res = concatenate(GTEx_gene_tissue(gene, tissue, snp_hash) for tissue in tissues)

	if DEBUG:
		print "\tFound %i genes associated to gene %s in GTEx" % (len(res), gene.id)

	return res

def GTEx_gene_tissue(gene, tissue, snp_hash):
	"""

		Returns all SNPs associated to a gene in GTEx in a given tissue
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		* { rsID: rsID }
		Returntype: [ Cisregulatory_Evidence ]

	"""

	
	server = "http://193.62.54.30:5555"
	ext = "/eqtl/id/%s/%s?content-type=application/json;statistic=p-value;tissue=%s" % ('homo_sapiens', gene.id, tissue); 
	try:
		eQTLs = get_rest_json(server, ext) 

		'''
			Example return object:
			[
				{
					'value': '0.804108648395327',
					'snp': 'rs142557973'
				},
			]
		'''

		res = [
			Cisregulatory_Evidence(
				snp = snp_hash[eQTL['snp']],
				gene = gene,
				tissue = tissue,
				score = float(eQTL['value']),
				source = "GTEx",
				study = None
			)
			for eQTL in eQTLs if eQTL['snp'] in snp_hash
		]

		if DEBUG:
			print "\tFound %i SNPs associated to gene %s in tissue %s in GTEx" % (len(res), gene.id, tissue)

		return res
	except:
		return None

def VEP(ld_snps, tissues):
	"""

		Returns all genes associated to a set of SNPs in VEP 
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: [ Regulatory_Evidence ]

	"""
	server = "http://rest.ensembl.org"
	ext = "/vep/%s/id" % (SPECIES)
	list = get_rest_json(server, ext, data = {"ids" : [snp.rsID for snp in ld_snps]})
	'''

		Example output from VEP:
		[
			{
				'colocated_variants': [
					{
						'phenotype_or_disease': 1,
						'seq_region_name': '9',
						'eas_allele': 'C',
						'amr_maf': '0.4553',
						'strand': 1,
						'sas_allele': 'C',
						'id': 'rs1333049',
						'allele_string': 'G/C',
						'sas_maf': '0.4908',
						'amr_allele': 'C',
						'minor_allele_freq': '0.4181',
						'afr_allele': 'C',
						'eas_maf': '0.5367',
						'afr_maf': '0.2133',
						'end': 22125504,
						'eur_maf': '0.4722',
						'eur_allele': 'C',
						'minor_allele': 'C',
						'pubmed': [
							24262325,
						],
						'start': 22125504
					}
				],
				'assembly_name': 'GRCh38',
				'end': 22125504,
				'seq_region_name': '9',
				'transcript_consequences': [
					{
						'gene_id': 'ENSG00000240498',
						'variant_allele': 'C',
						'distance': 4932,
						'biotype': 'antisense',
						'gene_symbol_source': 'HGNC',
						'consequence_terms': [
							'downstream_gene_variant'
						],
						'strand': 1,
						'hgnc_id': 'HGNC:34341',
						'gene_symbol': 'CDKN2B-AS1',
						'transcript_id': 'ENST00000584020',
						'impact': 'MODIFIER'
					},
				],
				'strand': 1,
				'id': 'rs1333049',
				'most_severe_consequence': 'downstream_gene_variant',
				'allele_string': 'G/C',
				'start': 22125504
			}
		]

	'''

	snp_hash = dict( (snp.rsID, snp) for snp in ld_snps)
	transcript_consequences = filter(lambda X: 'transcript_consequences' in X, list)
	res = [
		Cisregulatory_Evidence(
			snp = snp_hash[hit['input']],
			gene = get_ensembl_gene(consequence['gene_id']),
			score = VEP_impact_to_score[consequence['impact']],
			source = "VEP",
			study = None,
			tissue = None
		)
		for hit in transcript_consequences for consequence in hit['transcript_consequences'] 
	]

	if DEBUG:
		print "\tFound %i interactions in VEP" % (len(res))

	return res

def Fantom5(ld_snps, tissues):
	"""

		Returns all genes associated to a set of SNPs in Fantom5 
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: [ Regulatory_Evidence ]

	"""
	intersection = overlap_snps_to_bed(ld_snps, DATABASES_DIR + "/Fantom5.txt")
	fdr_model = pickle.load(open(DATABASES_DIR + "/Fantom5.fdrs"))
	snp_hash = dict( (snp.rsID, snp) for snp in ld_snps)
	res = filter (lambda X: X.score, (get_fantom5_evidence(feature, fdr_model, snp_hash) for feature in intersection))

	if DEBUG:
		print "\tFound %i interactions in Fantom5" % (len(res))

	return res

def get_fantom5_evidence(feature, fdr_model, snp_hash):
	'''
		Parse output: first 12 columns are from Fantom5 file, the next 4 are LD SNP coords
		1.	chrom
		2.	chromStart
		3.	chromEnd
		4.	name ";" separated: 
			1. chrom:start-end
			2. Refseq
			3. HGNC
			4. R:r2
			5. FDR:fdr
		5.	score
		6.	strand
		7.	thickStart
		8.	thickEnd
		9.	itemRgb
		10.	blockCount
		11.	blockSizes
		12.	chromStarts

		6. chrom
		7. start
		8. end
		9. rsID
	'''
	association_data = feature[3].split(";")

	gene = get_gene(association_data[2])
	snp = snp_hash[feature[15]]
	score = STOPGAP_FDR(snp, gene, fdr_model)

	return [
			Cisregulatory_Evidence(
				snp = snp,
				gene = gene,
				source = "Fantom5",
				score = score
			)
			for feature in intersection
		]

def DHS(ld_snps, tissues):
	"""

		Returns all genes associated to a set of SNPs in DHS 
		Args:
		* [ SNP ]
		* [ string ] (tissues)
		Returntype: [ Regulatory_Evidence ]

	"""
	intersection = overlap_snps_to_bed(ld_snps, DATABASES_DIR + "/DHS.txt")
	fdr_model = pickle.load(open(DATABASES_DIR+"/DHS.fdrs"))
	snp_hash = dict( (snp.rsID, snp) for snp in ld_snps)
	res = filter (lambda X: X.score, (get_dhs_evidence(line, fdr_model, snp_hash) for feature in intersectionr))

	if DEBUG:
		print "\tFound %i gene associations in DHS" % len(res)

	return res

def get_dhs_evidence(feature, fdr_model, snp_hash):
	"""

		Parse output of bedtools searching for DHS evidence
		Args:
		* string
		Returntype: Regulatory_Evidence

	"""
	'''

		First 5 columns are from DHS file, the next 4 are LD SNP coords
		Format of DHS correlation files:
		1. chrom
		2. chromStart
		3. chromEnd
		4. HGNC
		5. Correlation

		6. chrom
		7. start
		8. end
		9. rsID

	'''
	gene = get_gene(feature[3])
	snp = snp_hash[feature[8]]
	score = STOPGAP_FDR(snp, gene, fdr_model)

	return Regulatory_Evidence(
		snp = snp,
		gene = gene,
		source = "DHS",
		score = score,
	)

def STOPGAP_FDR(snp, gene, fdr_model):
	"""

		Special function for cis-regulatory interactions 
		Args:
		* rsID
		* ENSG stable ID
		* 
		Returntype: scalar

	"""

	if gene.chrom != snp.chrom:
		return 0

	distance = abs(snp.pos - gene.tss)

	if distance > fdr_model.MAX_DISTANCE:
		return 0

	bin = int(distance / fdr_model.BIN_WIDTH)

	if bin not in fdr_model.FDR:
		return 0

	FDR = fdr_model.FDR[bin]

	if FDR is None:
		return 0
	elif FDR < .6:
		return 2
	elif FDR < .85:
		return 1
	else:
		return 0

def get_gene(gene_name):
	"""

		Get gene details from name
		* string
		Returntype: Gene

	"""
	server = "http://rest.ensembl.org"
	ext = "/lookup/symbol/%s/%s?content-type=application/json;expand=1" % (SPECIES, gene_name)
	hash = get_rest_json(server, ext)
	return Gene(
		name = gene_name,
		id = hash['id'],
		chrom = hash['seq_region_name'],
		tss = int(hash['start']) if hash['strand'] > 0 else int(hash['end']),
		biotype = hash['biotype']
		)

def get_ensembl_gene(gene_name):
	"""

		Get gene details from name
		* string
		Returntype: Gene

	"""
	server = "http://rest.ensembl.org"
	ext = "/lookup/id/%s?content-type=application/json;expand=1" % (gene_name)
	hash = get_rest_json(server, ext)
	return Gene(
		name = gene_name,
		id = hash['id'],
		chrom = hash['seq_region_name'],
		tss = int(hash['start']) if hash['strand'] > 0 else int(hash['end']),
		biotype = hash['biotype']
		)

def get_snp_locations(rsID):
	"""

		Get SNP details from rsID 
		* string
		Returntype: SNP

	"""

	server = "http://rest.ensembl.org"
	ext = "/variation/%s/%s?content-type=application/json" % (SPECIES, rsID)
	hash = get_rest_json(server, ext)

	'''
		Example response:
		{
			'mappings': [
				{
					'assembly_name': 'GRCh38', 
					'end': 130656773, 
					'start': 130656773, 
					'coord_system': 'chromosome', 
					'allele_string': 'C/T', 
					'seq_region_name': '9', 
					'location': '9:130656773-130656773', 
					'strand': 1
				}
			], 
			'var_class': 'SNP', 
			'minor_allele': 'T', 
			'evidence': [
				'Multiple_observations', 
				'Frequency', 
				'HapMap', 
				'1000Genomes'
			], 
			'source': 'Variants (including SNPs and indels) imported from dbSNP', 
			'synonyms': [
				'rs60356236'
			], 
			'ambiguity': 'Y', 
			'MAF': '0.148962', 
			'ancestral_allele': 'T', 
			'most_severe_consequence': 'downstream_gene_variant', 
			'name': 'rs7028896'
		}
	'''
	return [
		SNP(
			rsID = rsID,
			chrom = mapping['seq_region_name'],
			pos = (int(mapping['start']) + int(mapping['end'])) / 2
		)
		for mapping in hash['mappings']
	]

def regulatory_evidence(snps, tissues):
	"""

		Extract regulatory evidence linked to SNPs and stores them in a hash
		* [ SNP ]
		* [ string ]
		Returntype: [ Regulatory_Evidence ]

	"""
	if DEBUG:
		print "Searching for regulatory data on %i SNPs in all databases" % (len(snps))

	res = concatenate(function(snps, tissues) for function in snp_regulatory_functions)

	if DEBUG:
		print "Found %i regulatory SNPs among %i in all databases" % (len(res), len(snps))

	# Group by SNP
	hash = collections.defaultdict(list)
	for hit in res:
		hash[hit.snp].append(hit)

	return hash

def GERP(snps, tissues):
	"""

		Extract GERP score at position
		Args:
		* [ SNP ] 
		Returntype: [ Regulatory_Evidence ]

	"""
	return map(GERP_at_snp, snps)

def GERP_at_snp(snp):
	"""

		Extract GERP score at position
		Args:
		* rsID
		Returntype: Regulatory_Evidence
	"""
	server = "http://rest.ensembl.org"
	ext = "/vep/human/id/%s?content-type=application/json;Conservation=1" % snp.rsID
	obj = get_rest_json(server, ext)
	return Regulatory_Evidence( 
			snp = snp,
			score = float(obj['Conservation']), # TODO: score on FDR?
			source = 'GERP'
	)

def Regulome(ld_snps, tissues):
	"""

		Extract Regulome score at sns of interest 
		Args:
		* [ SNP ]
		* [ string ]
		Returntype: [ Regulatory_Evidence ]

	"""
	snp_hash = dict( (snp.rsID, snp) for snp in ld_snps)
	intersection = overlap_snps_to_bed(ld_snps, DATABASES_DIR + "/Regulome.txt")
	res = filter (lambda X: X.score, get_regulome_evidence(intersection, snp_hash))

	if DEBUG:
		print "\tFound %i regulatory variants in Regulome" % (len(res))

	return res

def overlap_snps_to_bed(ld_snps, bed):
	'''
		Find overlaps between SNP elements and annotated Bed file
		Args:
		* [ SNP ]
		* string (location of bed file)
		Returntype: pybedtools Interval iterator

	'''
	SNP_string = "\n".join(
		( "\t".join((snp.chrom, str(snp.pos), str(snp.pos+1), snp.rsID)) for snp in ld_snps )
	)
	SNP_bt = pybedtools.BedTool(SNP_string, from_string=True)
	Annotation_bt = pybedtools.BedTool(bed)
	return SNP_bt.intersect(Annotation_bt, stream=True, wab=True)

def get_regulome_evidence(intersection, snp_hash):
	"""

		Extract Regulome score from bedtools output 
		Args:
		* string
		Returntype: Regulatory_Evidence

	"""

	'''
	First 4 columns are from Regulome file, the next 4 are LD SNP coords
	Regulome file format:
	1. chrom
	2. start
	3. end
	4. category

	LD SNP coords:
	5. chrom
	6. start
	7. end
	8. rsID
	'''
	return [
		Regulatory_Evidence(
			snp = snp_hash[feature[7]],
			source = "DHS",
			score = 2 if feature[3][0] == '1' or feature[3][0] == '2' else 1
		)
		for feature in intersection
	]

def gene_to_MeSH(gene):
	"""

		Look up MeSH annotations for gene
		Args:
		* [ string ] (gene names)
		Return type: [ string ] (annotations)
	"""
	server = "http://gene2mesh.ncibi.org"
	ext = "/fetch?genesymbol=%s&taxid=%s" % (gene.name, NCBI_Taxon_ID[SPECIES])
	response = requests.get(str(server)+str(ext))

	if not response.ok:
		sys.stderr.write("Failed to get proper response to query %s%s\n" % (server, ext) ) 
		sys.stderr.write(response.content + "\n")
		response.raise_for_status()
		sys.exit()

	'''
	Example MeSH output:

	{
		'Gene2MeSH': {
			'Request': {
				'ParameterSet': {
					'Tool': 'none',
					'GeneSymbol': 'csf1r',
					'TaxonomyID': '9606',
					'Limit': '1000',
					'Email': 'anonymous'
				},
				'type': 'fetch'
						},
						'Response': {
				'ResultSet': {
					'Result': [
						{
							'FisherExact': {
					'content': '1.8531319238671E-230',
					'type': 'p-value'
							},
							'ChiSquare': '112213.6506462',
							'Fover': '1498.1813411401',
							'MeSH': {
					'Qualifier': {
						'Name': 'metabolism'
					},
					'Descriptor': {
						'TreeNumber': [
							'D08.811.913.696.620.682.725.400.500',
							'D12.776.543.750.060.492',
							'D12.776.543.750.705.852.150.150',
							'D12.776.543.750.750.400.200.200',
							'D12.776.624.664.700.800',
							'D23.050.301.264.035.597',
							'D23.101.100.110.597'
						],
						'Identifier': 'D016186',
						'Name': 'Receptor, Macrophage Colony-Stimulating Factor',
						'UMLSID': {}
					}
							},
							'DocumentSet': {
					'type': 'pubmed',
					'PMID': [
					]
							},
							'Gene': {
					'Taxonomy': {
						'Identifier': '9606'
					},
					'Identifier': '1436',
					'type': 'Entrez',
					'Description': 'colony stimulating factor 1 receptor',
					'Symbol': 'CSF1R'
							}
						},
					],
					'sort': 'FisherExact',
					'count': '94',
					'order': 'ascending'
				},
				'Copyright': {
					'Details': 'http://nlp.ncibi.org/Copyright.txt',
					'Year': '2009',
					'Statement': 'Copyright 2009 by the Regents of the University of Michigan'
				},
				'Support': {
					'Details': 'http://www.ncibi.org',
					'GrantNumber': 'U54 DA021519',
					'Statement': 'Supported by the National Institutes of Health as part of the NIH\\\'s National Center for Integrative Biomedical Informatics (NCIBI)'
				}
			}
		}
	}
	'''

	if len(response.content):
		hash = xmltodict(response.content)
		hits = hash['Response']['ResultSet']['Result']
		return [hit['MeSH']['Descriptor']['Name'] for hit in hits]
	else:
		return []

def gene_to_phenotypes(gene):
	"""

		Look up phenotype annotations for gene
		Args:
		* ENSG stable ID
		Return type: [ OMIM Phenotype ]

	"""
	return [] # TODO and remove stopper
	if gene not in phenotype_cache:
		phenotype_cache[gene['stable_id']] = get_gene_phenotypes(gene)

	return phenotype_cache[gene['stable_id']]

def get_rest_json(server, ext, data=None):
	"""
		Args:
		* String (server name)
		* String (extension string)
		Return type: JSON object

	"""
	if data is None:
		headers = { "Content-Type" : "application/json" }
		r = requests.get(str(server)+str(ext), headers = headers)
	else:
		headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
		r = requests.post(str(server)+str(ext), headers = headers, data = json.dumps(data))

	if not r.ok:
		sys.stderr.write("Failed to get proper response to query %s%s\n" % (server, ext) ) 
		sys.stderr.write(r.content + "\n")
		r.raise_for_status()
		sys.exit()
	else:
		return r.json()

def concatenate(list):
	"""

		Shorthand to concatenate a list of lists
		Args: [[]]
		Returntype: []

	"""
	return sum(filter(lambda elem: elem is not None, list), [])

# List of databases used
database_functions = [GWASCatalog, GWAS_DB, Phewas_Catalog] # Removed GRASP for fast test
ld_snp_to_gene_functions = [GTEx, Fantom5, VEP]
snp_regulatory_functions = [Regulome] # TODO Implement and insert GERP code

main()
