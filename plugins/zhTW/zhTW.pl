# zhTW: show server text (NPC names, NPC dialogue and menus) in Traditional Chinese.
#
# Display only: console/log lines and the web panel's talk box are translated; the bot's own
# data (%talk, npc_steps matching, config) stays as the server sent it. NPC actor names are
# renamed, since nothing in config matches NPCs by name (npc_steps use coordinates/indexes).
# Dictionary: tables/rooff-zh/strings.tsv, built by ro-npc-zh-tw/tools/build.py.
# Untranslated NPC lines go to logs/zh-missing.txt for ro-npc-zh-tw/tools/harvest.py.
package zhTW;

use strict;
use utf8;
use Plugins;
use Settings;
use Log;
use Globals;

our %zh;
my %missed;
my $orig = \&Log::processMsg;

Plugins::register('zhTW', 'Traditional Chinese for server text', \&unload);
my $hooks = Plugins::addHooks(
	['start3', \&load],
	['npcNameUpdate', sub { rename_actor($_[1]{npc}) }],
	['add_npc_list', sub { rename_actor($_[1]) }],
	['mobNameUpdate', sub { mob($_[1]{monster}) }],
	['add_monster_list', sub { mob($_[1]) }],
);
load() if %Globals::monsters_lut;  # "plugin reload zhTW" in a running bot
{
	no warnings 'redefine';
	*Log::processMsg = sub {
		my @a = @_;
		$a[1] = tr_lines($a[1], $a[2]) if defined $a[1] && %zh;
		return $orig->(@a);
	};
}

sub unload {
	Plugins::delHooks($hooks);
	no warnings 'redefine';
	*Log::processMsg = $orig;
}

sub load {
	%zh = ();
	my $file = Settings::getTableFilename('strings.tsv') or return;
	open my $fh, '<:encoding(UTF-8)', $file or return;
	while (my $line = <$fh>) {
		chomp $line;
		my ($k, $v) = split /\t/, $line, 2;
		$zh{$k} = $v if defined $v;
	}
	close $fh;
	# monsters the server named in English (spawns missing from rooff-zh/monsters.txt)
	for my $id (keys %Globals::monsters_lut) {
		my $v = lookup($Globals::monsters_lut{$id});
		$Globals::monsters_lut{$id} = $v if defined $v;
	}
	if ($Globals::monstersList) { mob($_) for @{$Globals::monstersList->getItems} }
	if ($Globals::npcsList) { rename_actor($_) for @{$Globals::npcsList->getItems} }
	Log::message(sprintf("zhTW: %d strings\n", scalar keys %zh), 'system');
}

sub norm {
	my $s = shift;
	$s =~ s/\^[0-9A-Fa-f]{6}//g;
	$s =~ s/\s+/ /g;
	$s =~ s/^ | $//g;
	return $s;
}

# Translated text, or undef. ponytail: exact-line lookup; partial lines stay English.
sub lookup {
	my $s = shift;
	return undef unless defined $s;
	my $v = $zh{norm($s)};
	return defined $v ? strip($v) : undef;
}

sub strip { my $s = shift; $s =~ s/\^[0-9A-Fa-f]{6}//g; return $s }

sub rename_actor {
	my $actor = shift or return;
	my ($base, $tag) = ($actor->{name} // "") =~ /^([^#]*)(#.*)?$/;  # "Guard#izlude": unique tag kept
	my $v = lookup($base);
	$actor->setName($v . ($tag // "")) if defined $v;
}

sub mob {
	my $m = shift or return;
	my $v = lookup($m->{name}) // return;
	$m->setName($v);
	$Globals::monsters_lut{$m->{nameID}} = $v if defined $m->{nameID};
}

sub tr_lines {
	my ($msg, $domain) = @_;
	my @out;
	for my $line (split /(?<=\n)/, $msg) {
		my $nl = $line =~ s/\n$// ? "\n" : '';
		my $v;
		if (defined($v = lookup($line))) {
			$line = $v;
		} elsif ($domain eq 'npc' && $line =~ /^(.*?): (.+)$/) {
			my ($name, $text) = ($1, $2);
			my $t = lookup($text);
			miss($text) unless defined $t;
			$line = (lookup($name) // $name) . ': ' . ($t // $text);
		} elsif ($line =~ /^(\s*\d+\s+)(\S.*?)\s*$/ && defined($v = lookup($2))) {
			$line = $1 . $v;  # numbered list row, e.g. "talk resp" menu
		}
		push @out, $line . $nl;
	}
	return join '', @out;
}

sub miss {
	my $s = shift;
	return if $missed{$s}++ || $s !~ /[A-Za-z]{2}/;
	(my $q = $s) =~ s/(["\\])/\\$1/g;
	if (open my $fh, '>>:encoding(UTF-8)', "$Settings::logs_folder/zh-missing.txt") {
		print $fh qq{[zh-tw-ui] missing "$q"\n};
		close $fh;
	}
}

1;
