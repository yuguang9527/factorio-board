import com.demod.fbsr.BlueprintFinder;
import com.demod.fbsr.BlueprintFinder.FindBlueprintResult;
import com.demod.fbsr.bs.BSBlueprint;
import com.demod.fbsr.FBSR;
import com.demod.fbsr.Profile;
import com.demod.fbsr.RenderRequest;
import com.demod.fbsr.RenderResult;
import com.demod.dcba.CommandReporting;

import javax.imageio.ImageIO;
import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import java.util.Optional;

/**
 * FBSR bridge: renders Factorio blueprint strings to PNG.
 *
 * One-shot mode:
 *   java RenderBP <bp_string|@path/to/bp.txt> <out.png> [profile=vanilla]
 *
 * Daemon mode (stdin line protocol, JVM stays warm):
 *   java RenderBP -daemon [profile=vanilla]
 *   stdin:  <out_path>\t<bp_string>\n   (one job per line)
 *   stdout: OK <out_path>\n              or   ERR <message>\n
 */
public class RenderBP {
    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            System.err.println("Usage: RenderBP <bp|@file> <out.png> [profile]  OR  RenderBP -daemon [profile]");
            System.exit(2);
        }

        boolean daemon = "-daemon".equals(args[0]);
        String profileName;
        if (daemon) {
            profileName = args.length >= 2 ? args[1] : "vanilla";
        } else {
            profileName = args.length >= 3 ? args[2] : "vanilla";
        }

        Profile profile = Profile.byName(profileName);
        if (profile == null) {
            System.err.println("ERR profile not found: " + profileName);
            System.exit(3);
        }
        if (!FBSR.load(List.of(profile))) {
            System.err.println("ERR failed to load FBSR");
            System.exit(4);
        }

        try {
            if (daemon) {
                runDaemon();
            } else {
                String bpArg = args[0];
                String outPath = args[1];
                String bp = bpArg.startsWith("@")
                    ? Files.readString(Paths.get(bpArg.substring(1))).trim()
                    : bpArg;
                renderOne(bp, outPath);
                System.out.println("WROTE " + outPath);
            }
        } finally {
            FBSR.unload();
        }
    }

    private static void runDaemon() throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        // Signal readiness so the worker knows it can start dispatching
        System.out.println("READY");
        System.out.flush();
        String line;
        while ((line = in.readLine()) != null) {
            int tab = line.indexOf('\t');
            if (tab < 0) {
                System.out.println("ERR malformed job (expect out_path\\tbp_string)");
                System.out.flush();
                continue;
            }
            String outPath = line.substring(0, tab);
            String bp = line.substring(tab + 1);
            try {
                renderOne(bp, outPath);
                System.out.println("OK " + outPath);
            } catch (Exception e) {
                System.out.println("ERR " + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
            System.out.flush();
        }
    }

    private static void renderOne(String bpString, String outPath) throws Exception {
        List<FindBlueprintResult> found = BlueprintFinder.search(bpString);
        if (found.isEmpty() || found.get(0).blueprintString.isEmpty()) {
            throw new RuntimeException("failed to parse blueprint string");
        }
        BSBlueprint bp = found.get(0).blueprintString.get().findAllBlueprints().get(0);

        CommandReporting reporting = new CommandReporting(null, null, null);
        RenderRequest req = new RenderRequest(bp, reporting);
        req.setBackground(Optional.empty());
        req.setGridLines(Optional.empty());
        req.setDontClipSprites(true);

        RenderResult result = FBSR.renderBlueprint(req);
        File outFile = new File(outPath);
        File parent = outFile.getParentFile();
        if (parent != null) parent.mkdirs();
        ImageIO.write(result.image, "PNG", outFile);
    }
}
