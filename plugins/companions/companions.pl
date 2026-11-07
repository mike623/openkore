# companions: drive Ragnarok Offline population-engine companions from OpenKore.
#
# The server has no companion API: RO Offline's own Companions window types chat lines
# (@companion ... in public chat, stance/duty words in party chat) and reads back the
# "@CP|..." lines the server answers with. This plugin does the same.
#
# Command:  comp [list|summon <name>|dismiss <name>|remove <name>|stance atk|def|pass
#                |role <name> tank|supp|dd|roam [<name>] on|off|recall|recruit <player>]
# Config (all optional):
#   comp_autoSummon 1        after login/map load, summon saved companions that are not out
#   comp_stanceTown pass     stance sent on entering a town   (atk|def|pass, empty = leave)
#   comp_stanceField atk     stance sent on entering a non-town map
#   comp_partyName <name>    party created by `comp recruit`/auto summon when not in one
# Roster: $companions::roster (array of hashes), published in status.json by webstatus.
package companions;

use strict;
use Time::HiRes qw(time);
use JSON::PP ();
use Globals qw($char $field %config);
use Log qw(message warning);
use Commands;
use Plugins;
use Utils qw(timeOut);

our $wantSync; # time to run the post-login sync, 'summon' while waiting for the roster, or undef
Plugins::register('companions', 'Control RO Offline AI companions', \&unload);
my $hooks = Plugins::addHooks(
	['packet_pre/self_chat', \&onSelfChat],
	['in_game', sub { $wantSync = time + 3 }],
	['mainLoop_post', \&tick],
);
my $commands = Commands::register(['comp', 'RO Offline companions: comp help', \&cmdComp]);

our $roster = [];      # last complete @CP batch
our $updated;          # epoch of that batch
my @pending;           # rows of the batch being received
my @queue;             # [due time, console command]
my ($lastMap, $stance);

my %DUTY = (1 => 'tank', 2 => 'support', 3 => 'attacker');
my %STANCE = (atk => 'atk', attack => 'atk', def => 'def', defensive => 'def', pass => 'pass', passive => 'pass');
my %ROLE = (tank => 'tank', tk => 'tank', supp => 'supp', support => 'supp', dd => 'dd', attacker => 'dd');

sub unload { Plugins::delHooks($hooks); Commands::unregister($commands) }

# ponytail: server-side messages carry no "Name : " prefix, so packet_selfChat gives msg=undef;
# read the raw packet instead. A player cannot fake these: their lines always have the prefix.
sub onSelfChat {
	my (undef, $args) = @_;
	my $msg = $args->{message} // return;
	$msg =~ s/\0.*//s;
	return unless $msg =~ /^\@CP(END)?\|/;
	my @p = split /\|/, $msg, -1;
	if ($p[0] eq '@CPEND') {
		$roster = [@pending];
		@pending = ();
		$updated = time;
		autoSummon() if $wantSync && $wantSync eq 'summon';
		return;
	}
	push @pending, {
		name => $p[1], job => $p[7] || $p[2], lv => ($p[6] || $p[3]) + 0,
		active => b($p[4] eq '1'), favorite => b($p[5] eq '1'), out => b(($p[6] || 0) > 0),
		duty => $DUTY{$p[9] // ''}, heal_at => num($p[11]), emergency_at => num($p[12]),
		rest_below => num($p[13]), rest_until => num($p[14]),
		roam => ($p[15] // '') =~ /^[01]$/ ? b($p[15]) : undef, # undef = roaming disabled in Settings
		dead => b(($p[16] // '') eq '1'),
	};
}

sub b { $_[0] ? JSON::PP::true : JSON::PP::false }

sub num { defined $_[0] && $_[0] =~ /^-?\d+$/ && $_[0] >= 0 ? $_[0] + 0 : undef }

sub inParty { $char && $char->{party} && $char->{party}{joined} }

sub send_ { my ($cmd, $delay) = @_; push @queue, [time + ($delay || 0), $cmd] }
sub pub   { send_("c $_[0]", $_[1]) }
sub party { send_("p $_[0]", $_[1]) }

sub ensureParty {
	return 0 if inParty();
	my $name = $config{comp_partyName} || ($char ? "$char->{name}PT" : 'CompPT');
	$name = substr($name, 0, 23);
	send_(qq{party create "$name"});
	return 2; # seconds to wait before using it
}

sub autoSummon {
	undef $wantSync;
	my @todo = grep { !$_->{out} && $_->{active} } @$roster;
	return unless @todo;
	my $wait = ensureParty();
	pub("\@companion summon $_->{name}", $wait) for @todo;
	message "[companions] summoning ".join(', ', map { $_->{name} } @todo)."\n";
}

sub tick {
	if (@queue && $queue[0][0] <= time) {
		my $cmd = (shift @queue)->[1];
		Commands::run($cmd);
	}
	return unless $char && $field && $Globals::net && $Globals::net->getState() == Network::IN_GAME();
	if ($wantSync && $wantSync ne 'summon' && time >= $wantSync) {
		if ($config{comp_autoSummon}) { $wantSync = 'summon'; pub('@companion list raw') }
		else { undef $wantSync; pub('@companion list raw') }
	}
	my $map = $field->baseName;
	if (!defined $lastMap || $map ne $lastMap) {
		$lastMap = $map;
		my $want = $STANCE{lc($field->isCity ? $config{comp_stanceTown} // '' : $config{comp_stanceField} // '')};
		if ($want && inParty() && (!defined $stance || $stance ne $want)) {
			$stance = $want;
			party($want, 1);
			message "[companions] $map: stance $want\n";
		}
	}
}

sub cmdComp {
	my (undef, $args) = @_;
	my ($sub, @a) = split ' ', $args // '';
	$sub = lc($sub // 'list');
	if ($sub eq 'list') {
		if (!@$roster) { message "[companions] no roster yet, asking server\n" }
		for my $c (@$roster) {
			message sprintf("  %-14s %-16s lv%-3d %s%s%s%s%s\n", $c->{name}, $c->{job}, $c->{lv},
				$c->{out} ? 'OUT' : 'saved', $c->{dead} ? ' DEAD' : '', $c->{duty} ? " $c->{duty}" : '',
				$c->{roam} ? ' roaming' : '', $c->{favorite} ? ' *' : '');
		}
		message "  stance: ".($stance // 'unknown (default def)')."\n";
		pub('@companion list raw');
	} elsif ($sub =~ /^(summon|dismiss|remove|favorite|unfavorite|skills|gear)$/ && @a) {
		pub("\@companion $sub @a");
	} elsif ($sub eq 'stance' && $STANCE{lc($a[0] // '')}) {
		$stance = $STANCE{lc $a[0]};
		party($stance);
	} elsif ($sub eq 'role' && @a == 2 && $ROLE{lc $a[1]}) {
		party("$a[0] $ROLE{lc $a[1]}");
	} elsif ($sub eq 'roam' && @a && $a[-1] =~ /^(on|off)$/i) {
		pub('@companion roam '.join(' ', @a));
	} elsif ($sub eq 'recall') {
		party('recall');
	} elsif ($sub eq 'recruit' && @a) {
		my $who = join ' ', @a;
		my $wait = ensureParty();
		send_(qq{pm "$who" party}, $wait);
		send_($who =~ / / ? qq{party request "$who"} : "party request $who", $wait + 2);
		message "[companions] recruiting $who\n";
	} else {
		message "comp list | summon <name> | dismiss <name> | remove <name> | stance atk|def|pass\n"
			. "comp role <name> tank|supp|dd | roam [<name>] on|off | recall | recruit <player>\n";
	}
}

1;
