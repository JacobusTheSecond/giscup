#!/usr/bin/env python3
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from summary_run_impl import render_markdown

report = {
    'metadata': {'status':'complete_unverified','exit_code':0,'k':1000,'tau':0.75,'replica':0,'profile':'balanced','sa_seed':1,'ruin_seed':1,'revision':'R34','job_id':'7933','host':'node','submission_block_present':True},
    'verification': {'present':False,'feasible':None,'real_score':None,'preverify_cached_score':None,'preverify_score_delta':None,'runtime_seconds':None,'near_threshold_polygons':None,'threshold_margin_bands':{},'nearest_unqualified':[],'summary_path':'x'},
    'timeline': {'final_score':6117,'best_score':6117,'first_k_seconds':1,'elapsed_seconds':10,'accepted_moves':0,'accepted_by_phase':{},'score_gain_by_phase':{},'path':'timeline.csv'},
    'log': {'edge_pool_accepted':True,'expanded_universe_size':123,'remapped_restart_seeds':0,'candidate_counts':[10,123],'candidate_count':123,'coverage_intervals':1,'sa_phases':0,'sa_improvements':0,'sa_group_summaries':0,'sa_downhill_accepted':0,'sa_downhill_attempted':0,'sa_downhill_acceptance':None,'sa_reheats':0,'sa_phase_seconds_by_label':{},'post_k_sa_assigned_seconds':[],'repair_rounds_attempted':0,'repair_full_scans':0,'repair_partial_scans':0,'repair_partial_salvaged':0,'repair_admission_skips':0,'repair_evaluated_candidates':0,'repair_cached_pruned':0,'repair_cache_recomputed':0,'repair_scan_seconds':0,'repair_max_scan_seconds':0,'repair_max_predicted_scan_seconds':0,'repair_max_grace_seconds':0,'sa_current_best_divergence_samples':0,'sa_current_below_best_samples':0,'sa_temperature_roles':{},'sa_seed_allocations':[],'ruin_calls':0,'ruin_improvements':0,'ruin_attempts':0,'ruin_destroyed_min':None,'ruin_destroyed_max':None,'ruin_stagnation_level_max':0,'ruin_modes':{},'crossover_calls':0,'crossover_improvements':0,'portfolio_cycles':0,'portfolio_stagnation_max':0,'exploration_seeds_queued':0,'exploration_seeds_consumed':0,'exploration_seeds_pending':0,'exploration_score_drop_max_observed':0,'exploration_distance_min':None,'exploration_distance_max':None,'one_swap_moves':0,'checkpoints':1,'last_checkpoint_sequence':1,'last_checkpoint_score':6117,'operator_stats':{},'errors':[]},
    'resources': {},
    'recommendations': [],
    'result_summary': {'solver_final_score':6117,'solver_best_score':6117,'reported_score':6117,'canonical_score':None,'score_source':'optimization_timeline','independently_verified':False},
}
md = render_markdown(report)
assert '**Final optimizer score: `6117`**' in md, md
assert 'Canonical verification: **not run in this job**' in md, md
print('RINGARC34 final-score conclusion rendering: PASS')
