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
	@monsters_Killed $totalBaseExp $totalJobExp %itemChange $storeList %itemTypes_lut);
use Log qw(warning);
use Commands;
use Utils qw(timeOut calcPosition distance);
use Plugins;
use Settings;
use Network;
use AI;
use Skill;
use Scalar::Util ();

our $sellOpen = 0;
Plugins::register('webstatus', 'JSON status file for web panel', \&unload);
my $hooks = Plugins::addHooks(['mainLoop_post', \&tick], ['packet/identify_list', \&onIdentifyList],
	['postloadfiles', \&storeNextApply], ['AI_storage_auto_completed', \&storeNextDone],
	# NPC sell window: OpenKore drops %talk once the sell list arrives, so track it for the panel's shop.
	['packet/npc_sell_list', sub { $sellOpen = 1 }], ['packet/sell_result', sub { $sellOpen = 0 }],
	['Network::Receive::map_changed', sub { $sellOpen = 0 }]);
my $commands = Commands::register(
	['identify_item', 'Use a Magnifier and identify inventory item <#>', \&cmdIdentifyItem],
	['ic_set', 'Set an items_control.txt rule: ic_set <item id> <keep> <storage 0|1> <sell 0|1>', \&cmdIcSet],
	['shop_close', 'Close an NPC buy/sell window on the server too (canceltransaction/sell cancel can leave it open)', \&cmdShopClose],
	['store_next', 'Store an item on the next Kafra trip only: store_next <inventory #> | list | cancel [<inventory #>]', \&cmdStoreNext],
);
my $json = JSON::PP->new->canonical->utf8;
my %timer = (time => 0, timeout => 1);
my $start = time;
my (%gain, %last, %warned); # cumulative base/job/zeny gain since load

# An empty sell list is the one close the server always accepts ("Sell failed" is the expected reply);
# without it the server keeps the shop open and refuses walking and other NPCs.
sub cmdShopClose {
	@Globals::sellList = ();
	$sellOpen = 0;
	Misc::cancelNpcBuySell();
	$Globals::messageSender->sendSellBulk([]) if $Globals::messageSender;
	Log::message("Shop window closed.\n");
}

sub unload { Plugins::delHooks($hooks); Commands::unregister($commands) }

# store_next: like the sell list, but for Kafra storage. The item is made store-only in memory
# (%items_control, never the file), so OpenKore's own storage trip stores it and does not sell it
# first; the rules go back once a storage trip completes. Survives "reload items_control".
my %storeNext; # nameID => item name
sub storeNextApply {
	for my $id (keys %storeNext) {
		my $rule = {%{ Misc::items_control($storeNext{$id}, $id) }, storage => 1, sell => 0, keep => 0};
		$Globals::items_control{$id} = $rule;
		$Globals::items_control{lc $storeNext{$id}} = $rule if exists $Globals::items_control{lc $storeNext{$id}};
	}
}
sub storeNextDone {
	return unless %storeNext;
	Log::message("Store list done: " . join(', ', values %storeNext) . "\n");
	%storeNext = ();
	Commands::run('reload items_control'); # put the file's rules back
}
sub cmdStoreNext {
	my (undef, $arg) = @_;
	$arg = defined $arg ? $arg : '';
	if ($arg eq '' || $arg eq 'list') {
		return Log::message(%storeNext ? "Store list (next Kafra trip): " . join(', ', map { "$storeNext{$_} ($_)" } sort keys %storeNext) . "\n"
			: "Store list is empty\n");
	}
	if ($arg =~ /^cancel(?:\s+(\d+))?$/) {
		if (defined $1) {
			my $item = $char && $char->inventory->get($1) or return Log::error("No inventory item $1\n");
			delete $storeNext{$item->{nameID}};
		} else { %storeNext = () }
		Commands::run('reload items_control');
		return Log::message("Store list " . (defined $1 ? "updated" : "cleared") . "\n");
	}
	return Log::error("Usage: store_next <inventory #> | list | cancel [<inventory #>]\n") unless $arg =~ /^\d+$/;
	my $item = $char && $char->inventory->get($arg) or return Log::error("No inventory item $arg\n");
	return Log::error("$item->{name} is equipped\n") if $item->{equipped};
	$storeNext{$item->{nameID}} = $item->{name};
	storeNextApply();
	Log::message("Added to store list: $item->{name} ($arg); stored on the next Kafra trip\n");
}

# ic_set <item id> <keep> <storage> <sell>: replace (or append) that item's line in
# control/items_control.txt, keep a backup next to it, reload. Used by the panel's AI buttons.
sub cmdIcSet {
	my (undef, $arg) = @_;
	my ($id, $keep, $storage, $sell) = ($arg // '') =~ /^\s*(\d+)\s+(\d+)\s+([01])\s+([01])\s*$/
		or return Log::error("Usage: ic_set <item id> <keep> <storage 0|1> <sell 0|1>\n");
	my $file = Settings::getControlFilename('items_control.txt') or return Log::error("items_control.txt not found\n");
	open(my $in, '<:raw', $file) or return Log::error("Cannot read $file: $!\n");
	my @lines = <$in>; close $in;
	my $name = $itemsList ? '' : '';
	my $item = $char && $char->inventory ? $char->inventory->getByNameID($id) : undef;
	$name = $item->{name} if $item;
	my $new = "$id $keep $storage $sell" . ($name ne '' ? " # $name" : '') . "\n";
	my $done = 0;
	for (@lines) {
		if (!$done && /^\s*$id\s+\d/) {
			my ($old_comment) = /(#.*)$/;
			$_ = $name eq '' && $old_comment ? "$id $keep $storage $sell $old_comment\n" : $new;
			$done = 1;
		}
	}
	push @lines, ($lines[-1] =~ /\n\z/ ? '' : "\n") . $new unless $done;
	require File::Copy;
	File::Copy::copy($file, "$file.bak") or return Log::error("Backup failed: $!\n");
	open(my $out, '>:raw', $file) or return Log::error("Cannot write $file: $!\n");
	print $out @lines; close $out;
	Commands::run('reload items_control');
	my $c = Misc::items_control('', $id);
	Log::message("items_control: $id keep=$c->{keep} storage=$c->{storage} sell=$c->{sell}" . ($done ? " (updated)" : " (added)") . "\n");
}

# identify_item <inventory #>: one step instead of "is <magnifier>", "identify", "identify <n>".
# The Magnifier opens the server's identify list; when it arrives we pick the requested item.
my $identifyPending; # binID asked for, until the list arrives
sub cmdIdentifyItem {
	my (undef, $arg) = @_;
	return Log::error("Usage: identify_item <inventory #>\n") unless defined $arg && $arg =~ /^\d+$/;
	return Log::error("Not in game\n") unless $char && $char->inventory && $char->inventory->isReady;
	my $item = $char->inventory->get($arg);
	return Log::error("No inventory item $arg\n") unless $item;
	return Log::message("$item->{name} is already identified\n") if $item->{identified};
	# A list already open (a Magnifier used earlier, nothing picked): the server ignores new
	# Magnifiers until it is answered, so pick from it instead of using another one.
	if (grep { defined $_ && $_ eq $item->{binID} } @Globals::identifyID) {
		Log::message("Identifying $item->{name} ($arg) from the open identify list...\n");
		return $Globals::messageSender->sendIdentify($item->{ID});
	}
	my $magnifier = $char->inventory->getByNameID(611);
	return Log::error("No Magnifier in inventory\n") unless $magnifier;
	$identifyPending = $item->{binID};
	Log::message("Identifying $item->{name} ($arg) with a Magnifier...\n");
	$magnifier->use;
}
sub onIdentifyList {
	return unless defined $identifyPending;
	my $want = $identifyPending;
	undef $identifyPending;
	if (grep { defined $_ && $_ eq $want } @Globals::identifyID) {
		$Globals::messageSender->sendIdentify($char->inventory->get($want)->{ID});
	} else {
		Log::warning("Item $want is not in the identify list; type 'identify' to pick one\n");
	}
}

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
			field stats cart storage party companions guild homun merc pet talk deal exp_report);
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
			section(\%s, store_list => sub { [map { +{id => num($_), name => str($storeNext{$_})} } sort keys %storeNext] });
			section(\%s, sell_list => sub { [map { +{idx => num($_->{binID}), name => str($_->{name}), amount => num($_->{amount})} } @Globals::sellList] });
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
			section(\%s, companions => sub { $companions::updated ? {updated => int($companions::updated), members => $companions::roster} : undef });
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
				# zhTW plugin (if loaded) translates for display; responses are still picked by index
				my $zh = defined &zhTW::lookup ? sub { zhTW::lookup($_[0]) // $_[0] } : sub { $_[0] };
				return {npc => str($npc ? $npc->name : 'Unknown #' . unpack('V', $talk{ID})),
					messages => [map { str($zh->($_)) } @msg], state => $sm{$st} // ($st eq '' ? undef : $st),
					responses => [map { str($zh->($_)) } @{$talk{responses} || []}]};
			});
			section(\%s, shop => sub {
				my $st = $ai_v{npc_talk}{talk} // '';
				return {mode => 'buy', npc => str($storeList->{npcName}), items => [map { +{idx => num($_->{binID}),
					id => num($_->{nameID}), name => str($_->{name}), price => num($_->{price}), type => str($itemTypes_lut{$_->{type}})} }
					grep { $_ } @$storeList]} if $st eq 'store' && $storeList && $storeList->size;
				return {mode => 'choose'} if $st eq 'buy_or_sell';
				return {mode => 'sell'} if $sellOpen;
				undef;
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
