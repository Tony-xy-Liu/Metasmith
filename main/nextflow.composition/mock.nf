process pprodigal {
    container 'docker://quay.io/hallamlab/external_pprodigal:1.0.1'
    input:
        path contigs

    output:
        path 'orfs.gff3'

    script:
        """
        touch orfs
        """
}

process fastal {
    container 'docker://quay.io/hallamlab/fast_aligner:1.0'
    input:
        path orfs
        path fastal_protein_reference

    output:
        path 'annotations.csv'
}

workflow {
    aRk4 = pprodigal(contigs)
    
    splitLetters | flatten | convertToUpper | view { v -> v.trim() }
}