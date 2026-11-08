# pullMobs: gather monsters by hitting each one once, then fight the group.
#
#   pull on [N]   start: tag the nearest untagged monster (Throw Stone if you have Stones,
#                 else one melee hit), move on to the next, until N are chasing you;
#                 then attackAuto 2 until they are dead, then gather again
#   pull off      stop and put attackAuto back
#   pull          show state
#
# config.txt (all optional):
#   pullMobs_count 4      monsters to gather
#   pullMobs_stopHp 40    below this HP% stop pulling and teleport
#   pullMobs_dist 12      only tag monsters within this many cells
package pullMobs;

use strict;
use Plugins;
use Globals qw(%config $char $monstersList);
use Log qw(message);
use Misc qw(configModify);
use Utils qw(distance timeOut);
use AI;
use Skill;

use constant { STONE => 7049, THROW_STONE => 152 };

Plugins::register('pullMobs', 'hit monsters once to gather them, then fight', \&unload);
my $hooks = Plugins::addHooks(['AI_pre', \&tick]);
my $cmd = Commands::register(['pull', 'pull on [N] | pull off: gather monsters, then fight', \&command]);

my ($on, $phase, $saved, $target, $since, $last) = (0, 'pull', undef, undef, 0, 0);

sub unload { Plugins::delHooks($hooks); Commands::unregister($cmd); stop() if $on; }

sub want { $config{pullMobs_count} || 4 }

sub command {
	my (undef, $args) = @_;
	my ($what, $n) = split ' ', $args // '';
	if ($what eq 'on') {
		configModify('pullMobs_count', $n, 1) if $n && $n =~ /^\d+$/;
		$saved = $config{attackAuto} unless $on;
		($on, $phase, $target) = (1, 'pull', undef);
		configModify('attackAuto', 0, 1);
		message "[pullMobs] on: gathering ".want()." monsters\n", 'success';
	} elsif ($what eq 'off') {
		stop();
	} else {
		message "[pullMobs] ".($on ? "on, $phase, ".scalar(chasing())."/".want()." chasing" : 'off')."\n";
	}
}

sub stop {
	AI::clear('attack', 'skill_use') if $target;
	configModify('attackAuto', $saved // 2, 1);
	($on, $target) = (0, undef);
	message "[pullMobs] off\n", 'success';
}

# Monsters on you: Kore's aggressive list plus every one you have hit.
sub chasing {
	my %ids = map { $_ => 1 } AI::ai_getAggressives();
	$ids{$_->{ID}} = 1 for grep { $_->{dmgFromYou} > 0 && !$_->{dead} } @$monstersList;
	return keys %ids;
}

sub tick {
	return unless $on && $char && AI::state() == AI::AUTO && timeOut($last, 0.3);
	$last = time;

	if ($char->hp_percent < ($config{pullMobs_stopHp} || 40)) {
		message "[pullMobs] HP low: stop and teleport\n", 'warning';
		stop();
		Commands::run('tele');
		return;
	}

	my %chase = map { $_ => 1 } chasing();
	my $n = keys %chase;

	if ($phase eq 'fight') {
		return if $n;
		($phase, $target) = ('pull', undef);
		configModify('attackAuto', 0, 1);
		message "[pullMobs] group dead, gathering again\n";
		return;
	}
	if ($n >= want()) {
		AI::clear('attack', 'skill_use') if $target;
		($phase, $target) = ('fight', undef);
		configModify('attackAuto', 2, 1);
		message "[pullMobs] $n chasing: fight\n", 'success';
		return;
	}

	# Still tagging one: done once it took a hit or is coming at us; give up after 8 s.
	if ($target) {
		my $m = $monstersList->getByID($target);
		return if $m && !$m->{dead} && !$chase{$target} && !timeOut($since, 8);
		AI::clear('attack', 'skill_use');
		$target = undef;
	}

	my $max = $config{pullMobs_dist} || 12;
	my ($m) = sort { distance($char->{pos_to}, $a->{pos_to}) <=> distance($char->{pos_to}, $b->{pos_to}) }
		grep { !$chase{$_->{ID}} && !$_->{dead} && distance($char->{pos_to}, $_->{pos_to}) <= $max } @$monstersList;
	return unless $m;   # ponytail: nothing near; route_randomWalk/lockMap does the walking

	($target, $since) = ($m->{ID}, time);
	my $stone = $char->inventory->getByNameID(STONE)
		&& $char->getSkillLevel(Skill->new(idn => THROW_STONE));
	Commands::run($stone ? "sm ".THROW_STONE." $m->{binID} 1" : "a $m->{binID}");
}

1;
