# webstatus: writes $Settings::logs_folder/status.json about once a second for a web panel.
# Keys are the contract in openkore-probe/WEB-PANEL.md ("status.json (plugin)").
package webstatus;

use strict;
use JSON::PP;
use Encode ();
use Time::HiRes qw(time);
use Globals qw($char $field $net $monstersList $playersList $npcsList $portalsList $itemsList
	%jobs_lut %equipTypes_lut %statusName @skillsID @partyUsersID $accountID %guild %pet %talk %ai_v
	%currentDeal %incomingDeal %outgoingDeal %items_lut $startTime_EXP $startingzeny
	@monsters_Killed $totalBaseExp $totalJobExp %itemChange);
use Log qw(warning);
use Utils qw(timeOut calcPosition distance);
use Plugins;
use Settings;
use Network;
use AI;
use Skill;
use Scalar::Util ();

Plugins::register('webstatus', 'JSON status file for web panel', \&unload);
my $hooks = Plugins::addHooks(['mainLoop_post', \&tick]);
my $json = JSON::PP->new->canonical->utf8;
my %timer = (time => 0, timeout => 1);
my $start = time;
my (%gain, %last, %warned); # cumulative base/job/zeny gain since load

sub unload { Plugins::delHooks($hooks) }

# OpenKore normally stores names already decoded (I18N::bytesToString); only decode raw high bytes.
sub str {
	my $s = shift;
	return undef unless defined $s;
	return $s if Encode::is_utf8($s) || $s !~ /[\x80-\xff]/;
	my $d = eval { Encode::decode('big5', $s) };
	return defined $d ? $d : $s;
}

sub num { defined $_[0] && $_[0] ne '' ? $_[0] + 0 : undef }
sub bool { $_[0] ? JSON::PP::true : JSON::PP::false }

# ponytail: crude per-hour rates. Level-up handled by assuming we wrapped from the previous max.
sub track {
	my ($k, $cur, $max) = @_;
	return unless defined $cur;
	if (defined $last{$k}) {
		my ($p, $pmax) = @{$last{$k}};
		my $d = $cur - $p;
		$d = ($pmax || $p) - $p + $cur if $d < 0 && $k ne 'zeny' && $pmax;
		$gain{$k} += $d if $d > 0;
	}
	$last{$k} = [$cur, $max];
}

sub rate { my $h = (time - $start) / 3600; $h > 0 ? int(($gain{$_[0]} || 0) / $h) : undef }

# Run one section; on error log it and leave the key null so the rest of the file still gets written.
sub section {
	my ($s, $key, $code) = @_;
	my $v = eval { $code->() };
	if ($@) { warning "[webstatus] $key: $@" unless ($warned{$key} // "") eq $@; $warned{$key} = $@; $v = undef }
	$s->{$key} = $v;
}

sub item_type {
	my $it = shift;
	my $t = $it->{type};
	return 'card' if $t == 6;
	return 'arrow' if $t == 10 || $t == 16 || $t == 17 || $t == 19;
	return 'usable' if $it->usable;
	return 'equip' if $it->equippable;
	return 'etc' if $t == 3 || $t == 7;
	return 'other';
}

sub item_rec {
	my $it = shift;
	my $eq = $it->{equipped};
	return {idx => num($it->{binID}), id => num($it->{nameID}), name => str($it->{name}),
		amount => num($it->{amount}), type => item_type($it),
		equipped => $eq ? str($equipTypes_lut{$eq} || "slot $eq") : undef,
		identified => bool($it->{identified}), broken => bool($it->{broken}), usable => bool($it->usable)};
}

sub items_of { my $l = shift; [map { item_rec($_) } grep { $_ } @{$l->getItems}] }

# Nearest $n actors of an ActorList as {name,x,y,dist,idx} plus extra fields from $more.
sub nearby {
	my ($list, $pos, $n, $more) = @_;
	return [] unless $list && $pos;
	my @r;
	for my $a (@{$list->getItems}) {
		next unless $a && !$a->{dead};
		my $p = calcPosition($a) || $a->{pos_to} || $a->{pos} or next;
		my %h = (name => str($a->name), x => num($p->{x}), y => num($p->{y}),
			dist => sprintf('%.1f', distance($pos, $p)) + 0, idx => num($a->{binID}));
		%h = (%h, $more->($a)) if $more;
		push @r, \%h;
	}
	@r = sort { $a->{dist} <=> $b->{dist} } @r;
	return [@r[0 .. ($#r < $n - 1 ? $#r : $n - 1)]];
}

sub slave {
	my ($o, %k) = @_;
	return undef unless $o;
	my %r = (name => str(Scalar::Util::blessed($o) ? $o->name : $o->{name}));
	$r{$_} = num($o->{$k{$_} || $_}) for qw(lv hp hp_max sp sp_max hunger intimacy exp exp_max);
	return \%r;
}

sub deal_side {
	my $h = shift || {};
	[map { +{name => str($h->{$_}{name} || $items_lut{$_} || "Unknown $_"), amount => num($h->{$_}{amount})} } sort keys %$h];
}

sub tick {
	return unless timeOut(\%timer);
	$timer{time} = time;
	eval {
		my $state = !$net ? 'offline' : $net->getState() == Network::IN_GAME ? 'ingame'
			: $net->getState() == Network::NOT_CONNECTED ? 'offline' : 'connecting';
		my %s = (time => int(time), state => $state);
		$s{$_} = undef for qw(name lv job_lv job hp hp_max sp sp_max exp exp_max job_exp job_exp_max zeny
			weight weight_max map x y action target base_exp_hour job_exp_hour zeny_hour
			field stats cart storage party guild homun merc pet talk deal exp_report);
		$s{$_} = [] for qw(monsters statuses inventory skills players npcs portals items_ground);
		my $ai = AI::state();
		$s{ai} = $ai == AI::AUTO ? 'auto' : $ai == AI::MANUAL ? 'manual' : 'off';
		if ($state eq 'ingame' && $char) {
			$s{name} = str($char->{name});
			$s{job} = str($jobs_lut{$char->{jobID}});
			my %map = (lv => 'lv', job_lv => 'lv_job', hp => 'hp', hp_max => 'hp_max', sp => 'sp',
				sp_max => 'sp_max', exp => 'exp', exp_max => 'exp_max', job_exp => 'exp_job',
				job_exp_max => 'exp_job_max', zeny => 'zeny', weight => 'weight', weight_max => 'weight_max');
			$s{$_} = num($char->{$map{$_}}) for keys %map;
			$s{map} = $field ? $field->baseName : undef;
			my $pos = calcPosition($char) || $char->{pos_to};
			($s{x}, $s{y}) = ($pos->{x}, $pos->{y}) if $pos;
			$s{action} = AI::action();
			my $i = AI::findAction('attack');
			if (defined $i) {
				my $m = $monstersList->getByID(AI::args($i)->{ID});
				$s{target} = str($m->name) if $m;
			}
			section(\%s, monsters => sub { nearby($monstersList, $pos, 15, sub {
				my $m = shift; (hp_pct => defined $m->{hp_percent} ? int($m->{hp_percent}) : undef) }) });
			track('base', $char->{exp}, $char->{exp_max});
			track('job', $char->{exp_job}, $char->{exp_job_max});
			track('zeny', $char->{zeny});
			($s{base_exp_hour}, $s{job_exp_hour}, $s{zeny_hour}) = map { rate($_) } qw(base job zeny);

			section(\%s, field => sub { $field ? $field->sourceName : undef });
			section(\%s, stats => sub {
				my %st;
				$st{$_} = num($char->{$_}), $st{"${_}_bonus"} = num($char->{"${_}_bonus"}) for qw(str agi vit int dex luk);
				my %k = (points_free => 'points_free', skill_points_free => 'points_skill', atk => 'attack',
					atk_bonus => 'attack_bonus', matk_min => 'attack_magic_min', matk_max => 'attack_magic_max',
					def => 'def', def_bonus => 'def_bonus', mdef => 'def_magic', mdef_bonus => 'def_magic_bonus',
					hit => 'hit', flee => 'flee', flee_bonus => 'flee_bonus', critical => 'critical', aspd => 'attack_speed');
				$st{$_} = num($char->{$k{$_}}) for keys %k;
				$st{sex} = !defined $char->{sex} ? undef : $char->{sex} ? 'male' : 'female';
				\%st;
			});
			section(\%s, statuses => sub { [sort map { str($statusName{$_} || $_) } keys %{$char->{statuses} || {}}] });
			section(\%s, inventory => sub { $char->inventory && $char->inventory->isReady ? items_of($char->inventory) : [] });
			section(\%s, cart => sub {
				my $c = $char->cart;
				return undef unless $c && $c->isReady;
				return {items => items_of($c), count => num($c->items), count_max => num($c->items_max),
					weight => num($c->{weight}), weight_max => num($c->{weight_max})};
			});
			section(\%s, storage => sub {
				my $st = $char->storage;
				return undef unless $st && ($st->isReady || $st->wasOpenedThisSession);
				return {open => bool($st->isReady), items => items_of($st), count => num($st->items), count_max => num($st->items_max)};
			});
			section(\%s, skills => sub {
				my %tt = (Skill::TARGET_PASSIVE() => 'passive', Skill::TARGET_SELF() => 'self',
					Skill::TARGET_LOCATION() => 'location', Skill::TARGET_ENEMY() => 'actor', Skill::TARGET_ACTORS() => 'actor');
				my @r;
				for my $h (@skillsID) {
					my $sk = Skill->new(handle => $h);
					next unless $sk->getOwnerType == Skill::OWNER_CHAR;
					my $t = $sk->getTargetType;
					push @r, {handle => $h, id => num($sk->getIDN), name => str($sk->getName),
						lv => num($char->getSkillLevel($sk)), max_lv => undef,
						sp => num($char->{skills}{$h}{sp}), up => bool($char->{skills}{$h}{up}),
						target => defined $t ? ($tt{$t} || 'actor') : undef};
				}
				\@r;
			});
			section(\%s, players => sub { nearby($playersList, $pos, 30, sub { my $p = shift;
				(job => str($p->job), lv => num($p->{lv}),
				guild => $p->{guild} && defined $p->{guild}{name} ? str($p->{guild}{name}) : undef,
				party => $p->{party} && defined $p->{party}{name} ? str($p->{party}{name}) : undef) }) });
			section(\%s, npcs => sub { nearby($npcsList, $pos, 30) });
			section(\%s, portals => sub { nearby($portalsList, $pos, 30, sub { my $n = $_[0]->name;
				(dest => defined $n && $n =~ /->\s*(.+)$/ ? str($1) : undef) }) });
			section(\%s, items_ground => sub { nearby($itemsList, $pos, 30, sub { (amount => num($_[0]{amount})) }) });
			section(\%s, party => sub {
				my $p = $char->{party};
				return undef unless $p && $p->{joined};
				my @m;
				for my $id (grep { $_ ne '' && $p->{users}{$_} } @partyUsersID) {
					my $u = $p->{users}{$id};
					my $me = $id eq $accountID;
					push @m, {name => str($u->{name}), map => $me ? $s{map} : defined $u->{map} ? ($u->{map} =~ s/\.(gat|rsw)$//r) : undef,
						x => num($me ? $s{x} : $u->{pos}{x}), y => num($me ? $s{y} : $u->{pos}{y}),
						hp => num($me ? $char->{hp} : $u->{hp}), hp_max => num($me ? $char->{hp_max} : $u->{hp_max}),
						online => bool($me || $u->{online}), leader => bool($u->{admin})};
				}
				return {name => str($p->{name}), members => \@m};
			});
			section(\%s, guild => sub {
				my $n = $guild{name} // ($char->{guild} ? $char->{guild}{name} : undef);
				defined $n && $n ne '' ? {name => str($n), lv => num($guild{lv}), members_online => num($guild{conMember})} : undef;
			});
			section(\%s, homun => sub { $char->has_homunculus ? slave($char->{homunculus}, lv => 'level') : undef });
			section(\%s, merc => sub { $char->has_mercenary ? slave($char->{mercenary}, lv => 'level', intimacy => 'faith') : undef });
			section(\%s, pet => sub { %pet && defined $pet{name}
				? {%{slave(\%pet, lv => 'level', hunger => 'hungry', intimacy => 'friendly')}, name => str($pet{name})} : undef });
			section(\%s, talk => sub {
				return undef unless defined $talk{ID};
				my @msg = grep { $_ ne '' } split /\n/, ($talk{msg} // '');
				@msg = @msg[-10 .. -1] if @msg > 10;
				my $st = $ai_v{npc_talk}{talk} // '';
				my %sm = (select => 'responses', next => 'next', number => 'number', text => 'text', close => 'close');
				my $npc = $npcsList && $npcsList->getByID($talk{ID});
				return {npc => str($npc ? $npc->name : 'Unknown #' . unpack('V', $talk{ID})),
					messages => [map { str($_) } @msg], state => $sm{$st} // ($st eq '' ? undef : $st),
					responses => [map { str($_) } @{$talk{responses} || []}]};
			});
			section(\%s, deal => sub {
				if (%currentDeal) {
					my ($y, $o) = ($currentDeal{you_finalize}, $currentDeal{other_finalize});
					return {with => str($currentDeal{name}),
						state => $currentDeal{final} ? 'trading' : $y && $o ? 'finalized' : $y ? 'you_finalized'
							: $o ? 'other_finalized' : 'engaged',
						you => deal_side($currentDeal{you}), them => deal_side($currentDeal{other}),
						zeny_you => num($currentDeal{you_zeny}) // 0, zeny_them => num($currentDeal{other_zeny}) // 0};
				}
				return {with => str($incomingDeal{name}), state => 'incoming', you => [], them => [], zeny_you => 0, zeny_them => 0} if %incomingDeal;
				my $op = $playersList && $outgoingDeal{ID} && $playersList->getByID($outgoingDeal{ID});
				return {with => $op ? str($op->name) : undef, state => 'outgoing', you => [], them => [], zeny_you => 0, zeny_them => 0} if %outgoingDeal;
				undef;
			});
		}
		section(\%s, exp_report => sub {
			return undef unless $char && defined $startTime_EXP;
			return {seconds => int(time - $startTime_EXP), base => num($totalBaseExp) // 0, job => num($totalJobExp) // 0,
				zeny => defined $startingzeny && defined $char->{zeny} ? $char->{zeny} - $startingzeny : undef,
				monsters => [map { +{name => str($_->{name}), count => num($_->{count})} } grep { ref $_ } @monsters_Killed],
				items => [map { +{name => str($_), count => num($itemChange{$_})} } grep { $itemChange{$_} } sort keys %itemChange]};
		});
		my $file = "$Settings::logs_folder/status.json";
		open(my $fh, '>:raw', "$file.tmp") or die "open $file.tmp: $!";
		print $fh $json->encode(\%s);
		close $fh;
		rename("$file.tmp", $file) or die "rename: $!";
	};
	warning "[webstatus] $@" if $@;
}

1;
